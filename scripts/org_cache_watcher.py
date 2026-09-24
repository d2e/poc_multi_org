"""Keeps Vector's org id->name->tenant enrichment table
(opensearch/org-lookup.csv) in sync with the platform realm's live
organizations.

Observer, not poller: Keycloak already emits admin.ORGANIZATION-CREATE/UPDATE/
DELETE events through the same event-webhook mechanism Vector consumes (see
provision_webhook_opensearch.py, which registers this service as a second
webhook target). This service reacts to those events instead of re-polling the
admin API on a timer:

  1. On startup, does one bootstrap GET of /realms/{realm}/orgs to seed the
     cache with orgs that already existed before this service started.
  2. Runs an HTTP server that Keycloak POSTs every realm event to; events
     whose resourceType isn't exactly "ORGANIZATION" (e.g. membership changes,
     logins) are ignored.
  3. On each org CREATE/UPDATE/DELETE, updates an in-memory
     {id: {name, tenant}} dict (tenant from the org's own "tenant" custom
     attribute, see docs/multi-env-tenants.md) and rewrites the CSV.

Vector's http_server source and enrichment tables both hot-reload on file
change when started with --watch-config (stable since Vector 0.46), so no
restart is needed on either side of this pipeline.

Stdlib only (urllib + http.server), deliberately: this runs unmodified off the
stock python:3.12-alpine image with no build step or dependency install.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

KC_URL = os.environ.get("KC_URL", "http://keycloak:8080")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin")
PLATFORM_REALM = os.environ.get("PLATFORM_REALM", "platform")
CSV_PATH = Path(os.environ.get("ORG_LOOKUP_CSV", "/data/org-lookup.csv"))
PORT = int(os.environ.get("PORT", "2022"))
# HMAC secret this webhook was registered with (authType: hmac, see
# provision_webhook_opensearch.py) -- this endpoint previously accepted any
# POST with zero verification. See docs/opensearch-auth.md.
WEBHOOK_HMAC_SECRET = os.environ["WEBHOOK_HMAC_SECRET"]

ORG_RESOURCE_PATH = re.compile(r"^orgs/([0-9a-fA-F-]{36})$")

_lock = threading.Lock()
_orgs: dict[str, dict[str, str]] = {}  # org_id -> {"name": ..., "tenant": ...}


def _tenant_of(org: dict) -> str:
    """First value of the org's "tenant" custom attribute, or "" if untagged
    (org1/org2/org3/acme are standalone tenants, never tagged -- only the
    globex-* envs are). See docs/multi-env-tenants.md.
    """
    return (org.get("attributes") or {}).get("tenant", [""])[0]


def _post_form(url: str, fields: dict[str, str]) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as resp:
        return json.loads(resp.read())


def admin_token() -> str:
    body = _post_form(
        f"{KC_URL}/realms/master/protocol/openid-connect/token",
        {"client_id": "admin-cli", "username": ADMIN_USER, "password": ADMIN_PASS, "grant_type": "password"},
    )
    return body["access_token"]


def wait_for_keycloak(timeout_s: int = 180) -> None:
    print(f"waiting for keycloak at {KC_URL} ...", flush=True)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{KC_URL}/realms/master", timeout=5)
            print("keycloak is up", flush=True)
            return
        except (urllib.error.URLError, OSError):
            time.sleep(3)
    raise SystemExit("keycloak did not become ready in time")


def bootstrap(timeout_s: int = 180) -> None:
    """Seed the cache with orgs that already existed before this service started.

    Retries: the platform realm is created by a separate provisioning process
    (provision_platform_orgs.py) that may not have run yet when this container
    starts, so a 404 here is a legitimate startup race, not a fatal error.
    """
    url = f"{KC_URL}/realms/{PLATFORM_REALM}/orgs"
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {admin_token()}"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                orgs = json.loads(resp.read())
            break
        except (urllib.error.URLError, OSError) as exc:
            if time.monotonic() > deadline:
                raise SystemExit(f"platform realm never became available at {url}: {exc}")
            print(f"waiting for platform realm at {url} ...", flush=True)
            time.sleep(3)

    with _lock:
        for org in orgs:
            _orgs[org["id"]] = {"name": org["name"], "tenant": _tenant_of(org)}
        write_csv()
    print(f"bootstrapped {len(orgs)} orgs from {url}", flush=True)


def write_csv() -> None:
    """Caller must hold _lock.

    Writes in place (same inode) rather than write-temp-then-rename: Vector's
    --watch-config file watcher tracks the enrichment table's inode, so a
    rename-based swap is only ever picked up once -- the watch silently goes
    stale on every write after the first (confirmed: org created after the
    first swap resolved to a null org_name in OpenSearch despite the CSV on
    disk being correct). Writes are small (a handful of rows) so the
    non-atomicity window is negligible in practice.
    """
    with CSV_PATH.open("w") as f:
        f.write("id,name,tenant\n")
        for org_id, org in _orgs.items():
            f.write(f"{org_id},{org['name']},{org['tenant']}\n")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)

        # HMAC-SHA256 of the *raw* body, lowercase hex, in X-Keycloak-Signature
        # -- confirmed empirically against a real Keycloak-signed request, not
        # assumed from the extension's docs (which don't state the encoding).
        # compare_digest for a timing-safe comparison.
        expected = hmac.new(WEBHOOK_HMAC_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
        got = self.headers.get("X-Keycloak-Signature", "")
        if not hmac.compare_digest(got, expected):
            self.send_response(401)
            self.end_headers()
            return

        try:
            event = json.loads(raw_body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        if event.get("resourceType") == "ORGANIZATION":
            self._handle_org_event(event)

        self.send_response(204)
        self.end_headers()

    def _handle_org_event(self, event: dict) -> None:
        match = ORG_RESOURCE_PATH.match(event.get("resourcePath", ""))
        if not match:
            return
        org_id = match.group(1)
        op = event.get("operationType")

        with _lock:
            if op == "DELETE":
                _orgs.pop(org_id, None)
                print(f"org deleted: {org_id}", flush=True)
            else:
                representation = event.get("representation")
                try:
                    org = json.loads(representation) if representation else None
                except json.JSONDecodeError:
                    org = None
                name = org.get("name") if org else None
                if name:
                    tenant = _tenant_of(org)
                    _orgs[org_id] = {"name": name, "tenant": tenant}
                    print(f"org {op.lower() if op else 'updated'}: {org_id} -> {name} (tenant={tenant or '-'})", flush=True)
                else:
                    return
            write_csv()

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)


def main() -> None:
    wait_for_keycloak()
    bootstrap()
    print(f"listening on 0.0.0.0:{PORT}, writing {CSV_PATH}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
