"""Browser UI for the support side of the CIBA support-delegation demo (see
docs/ciba-support-delegation.md) -- the same flow `scripts/support_console.py`
drives from a terminal, as a web page instead.

Deliberately stateless on the backend: the only thing identifying a pending
request is Keycloak's own `auth_req_id`, round-tripped through the page's
URL/JS rather than a server-side session -- there's nothing else worth
remembering between polls, and it keeps this app free of the session-store
machinery `app/product_app.py` needs for its actual end-user login.

Polling can't block a browser request the way `support_console.py`'s
`time.sleep()` loop does, so it's inverted here: `/poll` makes exactly one
token-endpoint attempt and returns immediately; `/wait`'s page-side
JavaScript is what re-calls it on an interval (honoring Keycloak's own
`interval`/`slow_down`, same backoff `support_console.py` does) until the
request resolves or expires.
"""
from __future__ import annotations

import base64
import json
import os

import requests
from flask import Flask, redirect, render_template_string, request, url_for

KC_INTERNAL_URL = os.environ.get("KC_INTERNAL_URL", "http://keycloak:8080")
PLATFORM_REALM = "platform"
CLIENT_ID = "support-tool"
CLIENT_SECRET = "support-tool-secret-dev-only"
CIBA_GRANT_TYPE = "urn:openid:params:grant-type:ciba"

app = Flask(__name__)


def decode_jwt_claims(token: str) -> dict:
    """Decode a JWT's payload without verifying the signature -- fine here
    since this token was already obtained directly from Keycloak over a
    trusted server-to-server call, same reasoning as
    app/product_app.py's identical helper.
    """
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def normalize_binding_message(reason: str) -> str:
    """CIBA's binding_message rejects spaces and caps at 50 chars --
    confirmed from Keycloak's own validation error text, see
    docs/ciba-support-delegation.md. Auto-converted rather than rejected so
    a support agent can type a normal sentence into the form.
    """
    return "-".join(reason.split())[:50] or "support-access-request"


PAGE_STYLE = """
body { font-family: system-ui, sans-serif; max-width: 640px; margin: 2em auto; }
input[type=text] { width: 100%; padding: 0.5em; box-sizing: border-box; }
button { padding: 0.5em 1.5em; margin-top: 0.5em; }
pre { background: #f4f4f4; padding: 1em; border-radius: 4px; overflow: auto; }
.pending { color: #856404; }
.denied { color: #b00; }
.approved { color: #1b5e20; }
"""

FORM_TEMPLATE = """
<!doctype html><html><head><title>Support console</title><style>{{ style }}</style></head>
<body>
<h1>Support console</h1>
<p>Requests time-limited, user-approved access via CIBA -- see
<code>docs/ciba-support-delegation.md</code>.</p>
<form method="post" action="{{ url_for('start_request') }}">
  <label>User (email or username)</label>
  <input type="text" name="login_hint" placeholder="alice@acme.example.com" required autofocus>
  <label>Reason (shown to the user)</label>
  <input type="text" name="reason" placeholder="org profile access" value="org profile access">
  <button type="submit">Request access</button>
</form>
</body></html>
"""

WAIT_TEMPLATE = """
<!doctype html><html><head><title>Support console -- waiting</title><style>{{ style }}</style></head>
<body>
<h1>Support console</h1>
<p>Requesting access to <b>{{ login_hint }}</b>'s account ...</p>
<p id="status" class="pending">Waiting for approval (up to {{ expires_in }}s) ...</p>
<div id="result"></div>
<p><a href="{{ url_for('index') }}">New request</a></p>
<script>
let intervalMs = {{ interval }} * 1000;
const deadline = Date.now() + {{ expires_in }} * 1000;
const authReqId = {{ auth_req_id | tojson }};

function render(data) {
  const statusEl = document.getElementById("status");
  const resultEl = document.getElementById("result");
  if (data.status === "approved") {
    statusEl.textContent = "Approved.";
    statusEl.className = "approved";
    resultEl.innerHTML =
      "<h2>Decoded ID token</h2><pre>" + JSON.stringify(data.id_token_claims, null, 2) + "</pre>" +
      "<h2>Decoded access token</h2><pre>" + JSON.stringify(data.access_token_claims, null, 2) + "</pre>" +
      "<h2>Org memberships (via the delegated token)</h2><pre>" + JSON.stringify(data.orgs, null, 2) + "</pre>";
  } else if (data.status === "denied") {
    statusEl.textContent = "Denied by the user.";
    statusEl.className = "denied";
  } else if (data.status === "expired") {
    statusEl.textContent = "Request expired waiting for approval.";
    statusEl.className = "denied";
  } else if (data.status === "error") {
    statusEl.textContent = "Error: " + data.detail;
    statusEl.className = "denied";
  } else {
    if (data.slow_down) { intervalMs += 5000; }
    setTimeout(poll, intervalMs);
  }
}

function poll() {
  if (Date.now() > deadline) {
    render({status: "expired"});
    return;
  }
  fetch("{{ url_for('poll_request') }}?auth_req_id=" + encodeURIComponent(authReqId))
    .then(r => r.json())
    .then(render)
    .catch(err => render({status: "error", detail: String(err)}));
}

setTimeout(poll, intervalMs);
</script>
</body></html>
"""


@app.route("/")
def index():
    return render_template_string(FORM_TEMPLATE, style=PAGE_STYLE)


@app.route("/request", methods=["POST"])
def start_request():
    login_hint = request.form["login_hint"].strip()
    binding_message = normalize_binding_message(request.form.get("reason", ""))

    resp = requests.post(
        f"{KC_INTERNAL_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/ext/ciba/auth",
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"scope": "openid", "login_hint": login_hint, "binding_message": binding_message},
        timeout=10,
    )
    if resp.status_code != 200:
        return f"<p>Could not start the request: {resp.status_code} {resp.text}</p>", 400
    auth = resp.json()

    return redirect(
        url_for(
            "wait_page",
            login_hint=login_hint,
            auth_req_id=auth["auth_req_id"],
            interval=auth["interval"],
            expires_in=auth["expires_in"],
        )
    )


@app.route("/wait")
def wait_page():
    return render_template_string(
        WAIT_TEMPLATE,
        style=PAGE_STYLE,
        login_hint=request.args["login_hint"],
        auth_req_id=request.args["auth_req_id"],
        interval=int(request.args["interval"]),
        expires_in=int(request.args["expires_in"]),
    )


@app.route("/poll")
def poll_request():
    resp = requests.post(
        f"{KC_INTERNAL_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/token",
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": CIBA_GRANT_TYPE, "auth_req_id": request.args["auth_req_id"]},
        timeout=10,
    )
    if resp.status_code == 200:
        token = resp.json()
        orgs_resp = requests.get(
            f"{KC_INTERNAL_URL}/realms/{PLATFORM_REALM}/orgs",
            headers={"Authorization": f"Bearer {token['access_token']}"},
            timeout=10,
        )
        return {
            "status": "approved",
            "id_token_claims": decode_jwt_claims(token["id_token"]),
            "access_token_claims": decode_jwt_claims(token["access_token"]),
            "orgs": orgs_resp.json() if orgs_resp.ok else [],
        }

    error = resp.json().get("error", "unknown_error")
    if error == "authorization_pending":
        return {"status": "pending"}
    if error == "slow_down":
        return {"status": "pending", "slow_down": True}
    if error == "access_denied":
        return {"status": "denied"}
    return {"status": "error", "detail": error}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3001)
