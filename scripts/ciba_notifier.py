"""Keycloak's CIBA "Decoupled Auth Server" for this POC -- the piece Keycloak
itself deliberately does not ship a real implementation of (see
docs/ciba-support-delegation.md for the full writeup and how this contract
was confirmed against the actual running phasetwo-keycloak:26.6.6 image, not
assumed from docs).

When `support_console.py` (playing "support") starts a CIBA backchannel auth
request against the `support-tool` client with a target user's `login_hint`,
Keycloak needs to ask that user to approve it. Keycloak's built-in
`ciba-http-auth-channel` SPI provider does this by POSTing the pending
request to a configured URL -- this service is that URL. It:

  1. Receives that POST (`/notify`), holding the bearer token Keycloak sent
     (the callback authorization) in memory, keyed by `login_hint` -- the
     same identifier `app/product_app.py`'s logged-in user is polling on.
  2. Exposes `/pending/<login_hint>` for `product_app.py` to render a
     "support wants access -- approve/deny" banner for whoever is logged in
     under that login_hint.
  3. Exposes `/pending/<login_hint>/approve` and `/deny`, which relay the
     user's decision back to Keycloak's own
     `.../ext/ciba/auth/callback` endpoint using the *same* bearer token
     Keycloak handed this service in step 1 -- Keycloak correlates the
     pending request by decoding that token itself, not by anything this
     service invents.

Stdlib only (urllib + http.server), deliberately, matching
org_cache_watcher.py/audit_chain_signer.py: runs unmodified off the stock
python:3.12-alpine image, no build step or dependency install.

In-memory only, no persistence: a restart loses any pending (unapproved)
requests, which is fine here -- support_console.py's poll loop just times
out and the support agent retries, same as a real decoupled-auth backend
having a transient blip.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KC_INTERNAL_URL = os.environ.get("KC_INTERNAL_URL", "http://keycloak:8080")
PLATFORM_REALM = os.environ.get("PLATFORM_REALM", "platform")
PORT = int(os.environ.get("PORT", "2024"))

CALLBACK_URL = f"{KC_INTERNAL_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/ext/ciba/auth/callback"

# Matches provision_ciba_support.py's cibaExpiresIn (120s) plus a safety
# margin -- an entry left un-actioned past this is a CIBA request that has
# already expired on Keycloak's side (its device-code model is gone), so
# any later approve/deny would just 502 forever and leave the banner stuck
# in product-app with no way to clear it. Evicted lazily on next read
# rather than on a timer -- this service has no other reason to run a
# background thread.
PENDING_TTL_S = 130

_lock = threading.Lock()
# login_hint -> {"bearer_token": ..., "binding_message": ..., "received_at": ...}
_pending: dict[str, dict[str, str]] = {}


def _get_live_pending(login_hint: str) -> dict[str, str] | None:
    """Caller must hold _lock. Evicts and returns None if the entry is
    older than PENDING_TTL_S (see the constant's docstring)."""
    entry = _pending.get(login_hint)
    if entry and time.time() - entry["received_at"] > PENDING_TTL_S:
        del _pending[login_hint]
        return None
    return entry


def _send_json(handler: BaseHTTPRequestHandler, status: int, body: dict) -> None:
    payload = json.dumps(body).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _complete_ciba_request(login_hint: str, status: str) -> None:
    """Relays the user's approve/deny decision back to Keycloak using the
    exact bearer token Keycloak gave this service for this request (see
    module docstring point 3) -- Keycloak's callback endpoint decodes that
    token itself to find the pending request; this service never invents
    or forwards any request identifier of its own.
    """
    entry = _pending[login_hint]
    req = urllib.request.Request(
        CALLBACK_URL,
        method="POST",
        headers={
            "Authorization": f"Bearer {entry['bearer_token']}",
            "Content-Type": "application/json",
        },
        data=json.dumps({"status": status}).encode(),
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b""

        if path == "/notify":
            self._handle_notify(raw_body)
        elif path.startswith("/pending/") and path.endswith("/approve"):
            self._handle_decision(path[len("/pending/"):-len("/approve")], "SUCCEED")
        elif path.startswith("/pending/") and path.endswith("/deny"):
            self._handle_decision(path[len("/pending/"):-len("/deny")], "CANCELLED")
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/pending/"):
            self.send_response(404)
            self.end_headers()
            return
        login_hint = urllib.parse.unquote(path[len("/pending/"):])
        with _lock:
            entry = _get_live_pending(login_hint)
        if entry:
            _send_json(self, 200, {"pending": True, "binding_message": entry["binding_message"]})
        else:
            _send_json(self, 200, {"pending": False})

    def _handle_notify(self, raw_body: bytes) -> None:
        # Keycloak's ciba-http-auth-channel provider requires exactly this
        # bearer-token scheme and expects HTTP 201 back, or it treats the
        # whole notification as failed and 503s the backchannel auth
        # request that triggered it -- confirmed empirically, see
        # docs/ciba-support-delegation.md.
        auth_header = self.headers.get("Authorization", "")
        bearer_token = auth_header[len("Bearer "):] if auth_header.startswith("Bearer ") else ""
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        login_hint = body.get("login_hint", "")
        if login_hint and bearer_token:
            with _lock:
                _pending[login_hint] = {
                    "bearer_token": bearer_token,
                    "binding_message": body.get("binding_message", "Support is requesting access"),
                    "received_at": time.time(),
                }
            print(f"pending CIBA request for '{login_hint}': {body.get('binding_message')}", flush=True)

        self.send_response(201)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_decision(self, login_hint: str, status: str) -> None:
        login_hint = urllib.parse.unquote(login_hint)
        with _lock:
            has_pending = _get_live_pending(login_hint) is not None

        if not has_pending:
            _send_json(self, 404, {"error": f"no pending request for '{login_hint}'"})
            return

        try:
            _complete_ciba_request(login_hint, status)
            error = None
        except (urllib.error.URLError, OSError) as exc:
            error = str(exc)

        # Popped either way: a failed callback here means the underlying
        # CIBA request is unrecoverable (expired, or Keycloak rejected it),
        # so leaving the entry in place would just make every future
        # approve/deny 502 forever with no way to clear the banner.
        with _lock:
            _pending.pop(login_hint, None)

        if error:
            print(f"CIBA request for '{login_hint}' failed to resolve: {error}", flush=True)
            _send_json(self, 502, {"error": f"failed to reach keycloak: {error}"})
            return

        print(f"CIBA request for '{login_hint}' resolved: {status}", flush=True)
        _send_json(self, 200, {"result": status})

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)


def main() -> None:
    print(f"listening on 0.0.0.0:{PORT}, callback -> {CALLBACK_URL}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
