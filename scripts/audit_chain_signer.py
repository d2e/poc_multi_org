"""Tamper-evident hash chain for the keycloak-events audit log (see
docs/audit-log-integrity.md).

Sits between Keycloak's webhook and Vector: Keycloak posts every event here
instead of straight to Vector; this attaches a SHA-256 hash chain link to
each event (each event's hash covers its own raw content *and* the previous
event's hash, so altering or deleting any past event breaks the chain for
everything after it -- the standard "tamper-evident log" construction used
by Certificate Transparency and AWS CloudTrail log-file validation) and
forwards the enriched event on to Vector's actual ingest endpoint.

Why a separate service, not a Vector transform: VRL transforms are
stateless per event -- there's no built-in way to remember "what was the
previous event's hash" across invocations or restarts. That needs a
persistent, single-writer service, same shape as org_cache_watcher.py.

Stdlib only, same "no build step" design as org_cache_watcher.py.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# HMAC secret Keycloak's webhook signs incoming events with (separate from
# WEBHOOK_HMAC_SECRET_VECTOR below -- Keycloak no longer talks to Vector
# directly at all, only to this service).
INCOMING_HMAC_SECRET = os.environ["WEBHOOK_HMAC_SECRET_CHAIN"]
# HMAC secret this service signs its own forwarded requests to Vector with
# -- Vector's verify_signature transform doesn't care who the sender is,
# only that the signature matches this secret (see opensearch/vector.yaml).
OUTGOING_HMAC_SECRET = os.environ["WEBHOOK_HMAC_SECRET_VECTOR"]
VECTOR_URL = os.environ.get("VECTOR_URL", "http://vector:2021/log/ingest")
STATE_PATH = Path(os.environ.get("CHAIN_STATE_PATH", "/data/audit-chain-state.json"))
PORT = int(os.environ.get("PORT", "2023"))

# Vector's http_server source ACKs on receipt, *before* its elasticsearch
# sink has actually written anything downstream (confirmed -- see "Vector's
# http_server source always ACKs" in docs/opensearch-auth.md) -- so a 2xx
# response from the forward below is not proof the event survived. These
# back a short poll against OpenSearch directly, by the same "id" Vector's
# id_key promotes to _id, before this service commits its own chain state.
OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "https://opensearch:9200")
OPENSEARCH_CONFIRM_USER = os.environ.get("OPENSEARCH_CONFIRM_USER", "audit_confirmer")
OPENSEARCH_CONFIRM_PASSWORD = os.environ["OPENSEARCH_CONFIRM_PASSWORD"]
CONFIRM_RETRIES = int(os.environ.get("CONFIRM_RETRIES", "10"))
CONFIRM_DELAY_SECONDS = float(os.environ.get("CONFIRM_DELAY_SECONDS", "0.3"))

GENESIS_HASH = "0" * 64
# Bounded so a burst of retries can't grow this file unboundedly -- large
# enough to cover Keycloak's default retry backoff window (up to 15
# minutes, a handful of events/sec at most in this POC).
RECENT_CAP = 500

_lock = threading.Lock()
# OpenSearch's self-signed demo cert (same "-k" / verify=False treatment as
# every other internal caller in this project -- see docs/opensearch-auth.md).
_INSECURE_SSL_CONTEXT = ssl._create_unverified_context()
_CONFIRM_AUTH_HEADER = "Basic " + base64.b64encode(
    f"{OPENSEARCH_CONFIRM_USER}:{OPENSEARCH_CONFIRM_PASSWORD}".encode()
).decode()


def _confirm_indexed(event_id: str) -> bool:
    """Polls OpenSearch for the document Vector's sink is supposed to have
    written. Indexing is fast but not synchronous with Vector's ACK, so this
    retries briefly rather than checking once. A non-404 HTTP error (e.g. a
    permission/auth misconfiguration) is treated as a hard failure
    immediately -- retrying that 10 times wouldn't change the outcome.
    """
    url = f"{OPENSEARCH_URL}/keycloak-events/_doc/{urllib.parse.quote(event_id, safe='')}?_source=false"
    req = urllib.request.Request(url, headers={"Authorization": _CONFIRM_AUTH_HEADER})
    for attempt in range(CONFIRM_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=5, context=_INSECURE_SSL_CONTEXT):
                return True
        except urllib.error.HTTPError as e:
            if e.code != 404:
                return False
        except urllib.error.URLError:
            pass
        if attempt < CONFIRM_RETRIES - 1:
            time.sleep(CONFIRM_DELAY_SECONDS)
    return False


def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"seq": 0, "last_hash": GENESIS_HASH, "recent": {}}


def _save_state(state: dict) -> None:
    # Write-in-place (open-truncate-write), not rename-based -- matches the
    # atomic-write lesson already learned the hard way for org-lookup.csv
    # (see docs/gotchas.md): nothing here watches this file for hot-reload,
    # but there's no reason to risk the same class of bug for no benefit.
    STATE_PATH.write_text(json.dumps(state))


def _chain_link(raw_body: bytes, event_id: str) -> tuple[dict, dict]:
    """Returns (chain_fields_for_this_event, updated_state_to_persist).
    Idempotent for a retried delivery of the same event id (Keycloak's
    webhook retries the same underlying event with backoff -- the
    underlying event's own "id" stays the same across retries even though
    the extension's own per-delivery-attempt "uid" does not, per its docs)
    -- reuses the previously-assigned chain link instead of advancing the
    chain again, which would otherwise make a harmless retry look like a
    second, different event forever wedged in the chain.
    """
    state = _load_state()
    if event_id in state["recent"]:
        return state["recent"][event_id], state  # no state change -- not persisted again

    prev_hash = state["last_hash"]
    new_hash = hashlib.sha256(prev_hash.encode() + raw_body).hexdigest()
    new_seq = state["seq"] + 1
    fields = {"audit_seq": new_seq, "audit_prev_hash": prev_hash, "audit_hash": new_hash}

    state["seq"] = new_seq
    state["last_hash"] = new_hash
    state["recent"][event_id] = fields
    if len(state["recent"]) > RECENT_CAP:
        oldest_id = min(state["recent"], key=lambda k: state["recent"][k]["audit_seq"])
        del state["recent"][oldest_id]
    return fields, state


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)

        expected = hmac.new(INCOMING_HMAC_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
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

        event_id = event.get("id")

        # The ENTIRE read-compute-forward-persist cycle is one critical
        # section, not just the in-memory computation -- a hash chain is
        # inherently sequential (each link depends on the previous one
        # actually having been persisted), so two webhook deliveries handled
        # concurrently by ThreadingHTTPServer must be fully serialized here,
        # not just have their state-file reads/writes individually
        # locked. Confirmed empirically that a narrower lock (just around
        # _chain_link's compute step, forwarding done outside it) lets two
        # concurrent requests both read the same stale prev_hash/seq before
        # either persisted its result -- produced a duplicate audit_seq and
        # a broken chain link during setup.py's provisioning burst, caught
        # by scripts/verify_audit_chain.py itself, not assumed safe.
        with _lock:
            if not event_id:
                # Every real Keycloak USER/ADMIN event carries a stable "id"
                # -- confirmed empirically (see docs/audit-log-integrity.md).
                # One without it can't be chained; let it through unmodified
                # rather than dropping it, since blocking ingestion entirely
                # over a missing field on a POC would be a worse failure
                # mode than not chaining that one event.
                chain_fields, state_to_save = {}, None
            else:
                chain_fields, state_to_save = _chain_link(raw_body, event_id)

            outgoing = {
                **event,
                **chain_fields,
                # The exact bytes the hash was computed over, stored
                # verbatim so a verifier can recompute the hash
                # independently of whatever Vector's enrichment step adds
                # afterward (org_name, tenant, etc. never participate in
                # the hash).
                "audit_raw_body": raw_body.decode(),
            }
            outgoing_body = json.dumps(outgoing).encode()
            signature = hmac.new(OUTGOING_HMAC_SECRET.encode(), outgoing_body, hashlib.sha256).hexdigest()

            req = urllib.request.Request(
                VECTOR_URL,
                data=outgoing_body,
                headers={"Content-Type": "application/json", "X-Keycloak-Signature": signature},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=10):
                    pass
            except urllib.error.URLError:
                # Don't persist the advanced chain state if the forward
                # failed -- Keycloak retries on a non-2xx response, and the
                # next attempt (same event id) will see the SAME state on
                # disk and correctly redo this exact link rather than
                # skipping ahead.
                self.send_response(502)
                self.end_headers()
                return

            # A successful forward only proves Vector's source received the
            # request, not that its sink actually wrote the document (see
            # _confirm_indexed's docstring) -- without this, a sink-side
            # failure (backpressure, a dropped write) would still commit
            # this chain link and 204 Keycloak, permanently losing the event
            # with nothing but a sequence gap discovered whenever someone
            # next happens to run verify_audit_chain.py.
            if event_id and not _confirm_indexed(event_id):
                self.send_response(502)
                self.end_headers()
                return

            if state_to_save is not None:
                _save_state(state_to_save)

        self.send_response(204)
        self.end_headers()

    def log_message(self, format, *args) -> None:  # noqa: A002
        print(f"{self.address_string()} - {format % args}", flush=True)


def main() -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"listening on 0.0.0.0:{PORT}, chain state at {STATE_PATH}, forwarding to {VECTOR_URL}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
