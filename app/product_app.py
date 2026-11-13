"""Demo "product" web app for the subdomain-based IdP routing feature (see
docs/tenant-routing.md). Simulates a multi-tenant SaaS product where each
tenant gets a subdomain (acme.sample.127.0.0.1.nip.io); a visitor with no
session is sent straight to their org's federated IdP via Keycloak's
built-in `identity-provider-redirector` authenticator + the `kc_idp_hint`
query param -- no email prompt, no IdP picker.

Explicit OIDC endpoints (not discovery) on purpose: this app needs the same
KC_URL (browser-facing) / KC_INTERNAL_URL (container-to-container) split as
every other component in this project (see docs/architecture.md) --
discovery would only give us one base URL, which isn't enough here either.
"""
from __future__ import annotations

import base64
import json
import os
from urllib.parse import urlencode

import requests
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth
from functools import wraps

from flask import Flask, abort, redirect, request, session, url_for
from flask_session import Session

KC_URL = os.environ.get("KC_URL", "http://localhost:8082")
KC_INTERNAL_URL = os.environ.get("KC_INTERNAL_URL", "http://keycloak:8080")
PLATFORM_REALM = "platform"

# ciba_notifier.py -- see docs/ciba-support-delegation.md. index() polls
# this to show a "support wants access" approval banner; the approve/deny
# routes below relay the user's decision to it.
CIBA_NOTIFIER_URL = os.environ.get("CIBA_NOTIFIER_URL", "http://ciba-notifier:2024")

# Maps the first label of the request Host header to the Keycloak IdP alias
# to hint at. In this demo it's 1:1 with the org alias (see
# keycloak_admin.TENANTS in scripts/), but kept as an explicit mapping since
# a real app's tenant slugs and IdP aliases won't always match.
TENANT_IDP_ALIAS = {"acme": "acme", "globex": "globex-dev"}

# Which env-orgs to *offer* as active-organization switch targets once a
# tenant's environment is active -- see docs/multi-env-tenants.md. This is
# the app-level tenant-isolation enforcement point: Keycloak's
# switch-organization endpoint itself only checks membership, it has no
# concept of "tenant", so a user could in principle still be switched to an
# org outside this list via the raw API. This app never offers or accepts
# anything but these, but that's a UI-level restriction, not a server-side
# guarantee -- true isolation would need a custom Keycloak extension.
MULTI_ENV_TENANTS = {"globex": ["globex-dev", "globex-stage", "globex-prod"]}

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET_KEY", "dev-secret-not-for-production")

# Server-side sessions, not Flask's default signed-cookie session: the
# browser cookie only ever needs to carry an opaque session ID from here on.
# This isn't a style preference -- storing the actual OAuth tokens
# (access_token + id_token_raw + refresh_token) directly in a client-side
# cookie has a hard ceiling (~4093 bytes, browser-enforced) that this app
# hit TWICE in a row: first via a redundant decoded-claims copy, then again
# via a stale leftover key from an already-issued cookie that predated that
# fix. Both were real, but they're symptoms of the same architectural
# problem -- any future growth (more roles, more org attributes, a bigger
# realm_access claim) hits the same ceiling again. Moving the token data
# server-side removes the ceiling entirely rather than trimming individual
# fields each time it's hit. Filesystem-backed (not the in-memory default)
# so a container restart doesn't invalidate every open session, mirroring
# this project's existing "should work post-restart" convention for its
# other stateful services -- see docker-compose.yml's volume mount for
# SESSION_FILE_DIR.
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = os.environ.get("SESSION_FILE_DIR", "/data/flask_session")
app.config["SESSION_PERMANENT"] = False
# Secure gated on KC_URL's scheme rather than hardcoded True: this whole
# stack runs on plain HTTP locally (KC_URL=http://localhost:8082, this app
# on http://*.nip.io:3000) -- a browser will silently refuse to ever send a
# Secure cookie back over a non-HTTPS connection, which would break every
# login in this dev setup. KC_URL is already this project's existing
# "browser-facing URL" signal (see docs/architecture.md), so reusing it here
# means this becomes correct automatically the day KC_URL (and this app)
# actually move behind HTTPS, with no separate flag to remember to flip.
# SameSite=Lax (not the unset default) since nothing here needs cross-site
# cookie sending -- Keycloak's redirects back to this app are same-site
# top-level navigations, which Lax always allows.
app.config["SESSION_COOKIE_SECURE"] = KC_URL.startswith("https://")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
Session(app)

oauth = OAuth(app)
oauth.register(
    name="keycloak",
    client_id=os.environ["KC_CLIENT_ID"],
    client_secret=os.environ["KC_CLIENT_SECRET"],
    authorize_url=f"{KC_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/auth",
    access_token_url=f"{KC_INTERNAL_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/token",
    userinfo_endpoint=f"{KC_INTERNAL_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/userinfo",
    # authorize_access_token() validates the ID token signature itself and
    # needs this to fetch the signing keys -- without it, Authlib raises
    # RuntimeError('Missing "jwks_uri" in metadata') on every callback.
    jwks_uri=f"{KC_INTERNAL_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/certs",
    client_kwargs={"scope": "openid email profile"},
)


def decode_jwt_claims(token: str) -> dict:
    """Decode a JWT's payload without verifying the signature -- fine here
    since this app already got the token directly from Keycloak over a
    trusted server-to-server call, not from an untrusted client.
    """
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def current_tenant() -> str:
    """First label of the Host header, e.g. "acme" from
    "acme.sample.127.0.0.1.nip.io". Used only for pre-login routing
    (kc_idp_hint) -- what's actually displayed post-login is the
    active_organization claim from the token, which Keycloak itself
    determined (see docs/tenant-routing.md).
    """
    return request.host.split(":")[0].split(".")[0]


def store_tokens(token: dict) -> None:
    # Deliberately NOT also storing the decoded claims dict -- id_token_raw
    # already encodes the exact same information, just base64'd instead of
    # JSON-key-per-field, so keeping both is pure duplication. This used to
    # be safety-critical back when sessions were client-side signed cookies
    # (it pushed the cookie's signed value past browsers' ~4093-byte cookie
    # limit -- see docs/gotchas.md for the full incident); now that sessions
    # are server-side (see the Session(app) setup above), there's no hard
    # ceiling anymore, but it's still needless duplication. Decode claims on
    # demand from id_token_raw instead (see current_claims()).
    #
    # Still explicitly popped, not just "stopped being written": a session
    # created by an older build of this app (before this key was removed)
    # would otherwise carry a stale id_token_claims value forward forever --
    # server-side storage doesn't change that, since the session dict itself
    # is what's persisted, cumulatively, across requests.
    session.pop("id_token_claims", None)
    session["id_token_raw"] = token["id_token"]
    # access_token is needed as a bearer token for switch-organization and
    # the self-scoped GET /orgs lookup (see my_orgs()).
    session["access_token"] = token["access_token"]
    # Access tokens are short-lived by design (a few minutes) -- refresh_token
    # lets a still-open tab keep working past that without forcing a fresh
    # login, see refresh_access_token().
    if "refresh_token" in token:
        session["refresh_token"] = token["refresh_token"]


def current_claims() -> dict:
    return decode_jwt_claims(session["id_token_raw"])


def require_role(role: str):
    """Route decorator enforcing that the CURRENT active_organization's role
    includes `role` -- 403s otherwise. This is the enforcement point this
    app was missing entirely: active_organization.role was decoded and
    displayed on the index page, but nothing anywhere ever branched on it.
    A user's role is scoped to their *active* org specifically (see
    docs/multi-env-tenants.md) -- alice can be admin in globex-dev and
    viewer in globex-prod at the same time, so this always re-checks
    against whatever org is active right now, never a cached/stale value.
    """

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "id_token_raw" not in session:
                return redirect(url_for("login"))
            roles = current_claims().get("active_organization", {}).get("role") or []
            if role not in roles:
                abort(403, description=f"requires role '{role}' in the active organization")
            return view(*args, **kwargs)

        return wrapped

    return decorator


def refresh_access_token() -> bool:
    """Exchanges the stored refresh_token for a fresh token set. Returns
    False (session left untouched) if the refresh token itself is no
    longer valid either -- e.g. it also expired, or the Keycloak SSO
    session ended -- in which case the caller should send the user back to
    /login rather than retry again.
    """
    refresh_token = session.get("refresh_token")
    if not refresh_token:
        return False
    resp = requests.post(
        f"{KC_INTERNAL_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": os.environ["KC_CLIENT_ID"],
            "client_secret": os.environ["KC_CLIENT_SECRET"],
        },
        timeout=10,
    )
    if resp.status_code != 200:
        return False
    store_tokens(resp.json())
    return True


def my_orgs() -> list[dict]:
    """Orgs the current session's user is a member of, via keycloak-orgs'
    self-scoped GET /realms/{realm}/orgs -- it filters to the caller's own
    memberships when called with a plain user access token (confirmed
    empirically: no admin role/token needed), so this app never needs
    admin credentials just to know what to offer as switch targets.

    A stale access_token (expired while a tab sat open) is retried once
    after a token refresh rather than left to bubble up as an unhandled
    401 -> 500 -- confirmed as a real failure mode, not a hypothetical one:
    a tab left open past the access token's lifetime hit exactly this.
    """
    resp = requests.get(
        f"{KC_INTERNAL_URL}/realms/{PLATFORM_REALM}/orgs",
        headers={"Authorization": f"Bearer {session['access_token']}"},
        timeout=10,
    )
    if resp.status_code == 401 and refresh_access_token():
        resp = requests.get(
            f"{KC_INTERNAL_URL}/realms/{PLATFORM_REALM}/orgs",
            headers={"Authorization": f"Bearer {session['access_token']}"},
            timeout=10,
        )
    resp.raise_for_status()
    return resp.json()


def pending_support_request(login_hint: str) -> str | None:
    """Binding message of a pending CIBA support request for this user, or
    None -- see ciba_notifier.py and docs/ciba-support-delegation.md.
    Best-effort: if the notifier is briefly unreachable, no banner is shown
    rather than breaking the whole page.
    """
    try:
        resp = requests.get(f"{CIBA_NOTIFIER_URL}/pending/{login_hint}", timeout=5)
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException:
        return None
    return body.get("binding_message") if body.get("pending") else None


@app.after_request
def no_cache(response):
    # Every response here is session-specific (who's logged in, which org is
    # active) -- nothing in this app should ever be served from a cache.
    # Also matters for back/forward navigation specifically: without
    # no-store, hitting the browser's Back button after /logout can restore
    # the *previous* rendered page straight from the back-forward cache
    # (bfcache) without re-requesting it at all, showing "Welcome" again
    # even though the session was actually cleared -- looks exactly like
    # "logout didn't work" but is really just a stale cached render.
    # no-store is what actually disables bfcache in Chrome; the other
    # headers cover older/other browsers' plain HTTP caching.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def landing_page() -> str:
    """Reference index of every local demo URL + credential in this
    project, one place instead of digging through README/CLAUDE.md.
    Deliberately served from THIS app rather than a separate static file or
    container: visiting product-app with no recognized tenant subdomain
    (plain http://localhost:3000/) previously just 404'd as "Unknown
    tenant" -- this repurposes that otherwise-dead landing spot instead of
    adding new infrastructure just to host a links page.
    """
    rows = [
        ("Keycloak admin console", "http://localhost:8082", "admin / admin"),
        ("Keycloak account console (org1/org2/org3/shared.user)", "http://localhost:8082/realms/platform/account", "see rows below"),
        ("OpenSearch (REST API, TLS)", "https://localhost:9200", "admin / &lt;OPENSEARCH_INITIAL_ADMIN_PASSWORD&gt; -- break-glass only"),
        ("OpenSearch Dashboards", "http://localhost:5601", "log in via Keycloak: opensearch.admin / password123"),
        ("Events dashboard", "http://localhost:5601/app/dashboards#/view/keycloak-events-dashboard", "same as above"),
        ("Tenant routing demo (acme)", "http://acme.sample.127.0.0.1.nip.io:3000", "alice@acme.example.com / password123"),
        ("Multi-env tenant demo (globex)", "http://globex.sample.127.0.0.1.nip.io:3000", "alice@globex.example.com / password123 -- globex-dev (admin), switch to globex-stage (developer) / globex-prod (viewer)"),
        ("Support console (CIBA support-delegation demo)", "http://localhost:3001", "no login -- enter a user's email, they approve/deny from their own tab here"),
        ("AI Agent Delegation demo (RFC 8693)", "/ai-agent", "Interactive browser flow: login -> consent screen -> may_act -> token exchange -> act claim"),
    ]
    test_users = [
        ("alice@org1.example.com", "password123", "federated via org1-idp -> org1"),
        ("alice@org2.example.com", "password123", "federated via org2-idp -> org2"),
        ("alice@org3.example.com", "password123", "federated via org3-idp -> org3"),
        ("shared.user", "password123", "local user, member of org1 + org2 (via the account console above)"),
    ]
    rows_html = "".join(
        f"<tr><td>{label}</td><td><a href=\"{url}\">{url}</a></td><td>{creds}</td></tr>" for label, url, creds in rows
    )
    users_html = "".join(
        f"<tr><td>{u}</td><td>{p}</td><td>{note}</td></tr>" for u, p, note in test_users
    )
    # Kept in sync by hand with docs/architecture.md's own diagram -- this
    # is a rendering of the same topology, not a second source of truth for
    # it. Auth annotations (OIDC / HMAC / basic) reflect docs/opensearch-auth.md
    # and docs/audit-log-integrity.md. Rendered client-side by Mermaid
    # (loaded from a CDN) rather than hand-drawn ASCII -- real boxes/arrows/
    # colors, auto-laid-out, far more readable than a monospace diagram.
    mermaid_source = """\
flowchart TD
    Browser(["Browser"])
    ProductApp["p2-product-app :3000\\nFlask + Authlib\\nserver-side sessions"]
    Keycloak["p2-keycloak :8080 / :8082\\nrealm: platform\\norgs: acme, globex-dev/stage/prod\\nrealms: acme-idp, globex-idp"]
    Postgres[("p2-postgres\\npgdata volume")]
    ChainSigner["p2-audit-chain-signer :2023\\nverify HMAC to attach hash-chain link,\\nre-sign, forward"]
    Vector["p2-vector :2021\\nverify_signature to\\nenrich_org_name (VRL)"]
    OrgCache["p2-org-cache-watcher :2022\\nwrites org-lookup.csv"]
    OpenSearch[("p2-opensearch :9200 (TLS)\\nindex: keycloak-events\\nOIDC + vector_writer (create-only)")]
    Dashboards["p2-opensearch-dashboards :5601\\n'Log in with single sign-on'"]

    Browser -->|"acme.* / globex.*\\n.sample.127.0.0.1.nip.io:3000"| ProductApp
    ProductApp -->|"browser-facing\\nKC_URL :8082"| Keycloak
    Keycloak -->|"server-to-server\\nKC_INTERNAL_URL :8080"| ProductApp
    Keycloak --> Postgres
    Keycloak -->|"webhook, HMAC-signed"| ChainSigner
    Keycloak -->|"webhook, HMAC-signed"| OrgCache
    Keycloak -.->|"OIDC login (human)"| Dashboards
    ChainSigner -->|"webhook, HMAC-verified"| Vector
    OrgCache -.->|"org id to name/tenant\\nlookup, kept live"| Vector
    Vector -->|"basic auth\\ncreate-only, no delete/update"| OpenSearch
    OpenSearch -->|"kibanaserver basic auth\\n(Dashboards' own backend)"| Dashboards

    classDef browser fill:#e8f0fe,stroke:#4285f4,color:#1a237e
    classDef app fill:#fff3e0,stroke:#fb8c00,color:#e65100
    classDef idp fill:#e8f5e9,stroke:#43a047,color:#1b5e20
    classDef db fill:#f3e5f5,stroke:#8e24aa,color:#4a148c
    classDef pipeline fill:#e1f5fe,stroke:#0288d1,color:#01579b
    classDef search fill:#fff9c4,stroke:#f9a825,color:#e65100
    class Browser browser
    class ProductApp app
    class Keycloak idp
    class Postgres db
    class ChainSigner,Vector,OrgCache pipeline
    class OpenSearch,Dashboards search
"""
    return (
        "<h1>poc_multi_org -- local demo index</h1>"
        "<p>See docs/ in the repo for the full writeup of each feature.</p>"
        "<table border=\"1\" cellpadding=\"6\" style=\"border-collapse:collapse\">"
        "<tr><th>Service</th><th>URL</th><th>Credentials</th></tr>"
        f"{rows_html}</table>"
        "<h2>Additional test users (via the Keycloak account console above)</h2>"
        "<table border=\"1\" cellpadding=\"6\" style=\"border-collapse:collapse\">"
        "<tr><th>Username</th><th>Password</th><th>Notes</th></tr>"
        f"{users_html}</table>"
        "<h2>Architecture</h2>"
        "<p>See <code>docs/architecture.md</code>, <code>docs/opensearch-auth.md</code>, and "
        "<code>docs/audit-log-integrity.md</code> (tamper-evident hash chain, run "
        "<code>python3 scripts/verify_audit_chain.py</code> to check) for the full writeup.</p>"
        '<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>'
        f'<pre class="mermaid" style="background:#fafafa;padding:1em;border-radius:4px;">{mermaid_source}</pre>'
        "<script>mermaid.initialize({startOnLoad:true, securityLevel:'loose'});</script>"
    )


@app.route("/")
def index():
    if current_tenant() not in TENANT_IDP_ALIAS and "id_token_raw" not in session:
        return landing_page()
    if "id_token_raw" not in session:
        return redirect(url_for("login"))
    claims = current_claims()
    active_org = claims.get("active_organization", {})
    active_org_name = active_org.get("name", "(none)")
    active_role = active_org.get("role") or []
    pretty_claims = json.dumps(claims, indent=2)
    # Decoded on demand from session["access_token"] rather than stored
    # separately, same reasoning as current_claims() -- it's only ever
    # needed for this display, so storing a decoded copy in the session
    # would just be more duplicate bytes contributing to the same
    # cookie-size ceiling documented on store_tokens().
    pretty_access_claims = json.dumps(decode_jwt_claims(session["access_token"]), indent=2)

    switch_links = ""
    error = session.pop("switch_error", None)
    error_html = f'<p style="color:#b00">{error}</p>' if error else ""

    login_hint = claims.get("preferred_username")
    binding_message = pending_support_request(login_hint) if login_hint else None
    support_banner_html = ""
    if binding_message:
        support_banner_html = (
            '<p style="background:#fff3cd;border:1px solid #e6c200;padding:0.75em;border-radius:4px">'
            f"Support is requesting access to your account (reason: <b>{binding_message}</b>). "
            f'<a href="{url_for("support_approve")}">Approve</a> | '
            f'<a href="{url_for("support_deny")}">Deny</a></p>'
        )

    tenant = session.get("url_tenant")
    env_orgs = MULTI_ENV_TENANTS.get(tenant, [])
    if env_orgs:
        try:
            member_org_names = {o["name"] for o in my_orgs()}
        except requests.HTTPError:
            # Refresh already tried and failed inside my_orgs() -- the
            # Keycloak SSO session itself is gone (expired or logged out
            # elsewhere), not just this one access token. Nothing left to
            # do but ask the user to log in again.
            session.clear()
            return redirect(url_for("login"))
        links = []
        for env in env_orgs:
            if env == active_org_name:
                links.append(f"<b>{env}</b> (current)")
            elif env in member_org_names:
                links.append(f'<a href="{url_for("switch", org_name=env)}">{env}</a>')
            else:
                links.append(f"{env} (not a member)")
        switch_links = f"<p>Switch environment: {' | '.join(links)}</p>"

    return (
        f"<h1>Welcome, {claims.get('email', claims.get('preferred_username'))}</h1>"
        f"{support_banner_html}"
        f"{error_html}"
        f"<p>URL tenant (used to pick the IdP before login): <b>{session['url_tenant']}</b></p>"
        f"<p>Keycloak active_organization claim (authoritative, post-login): "
        f"<b>{active_org_name}</b>, role(s): <b>{', '.join(active_role) or '(none)'}</b></p>"
        f"{switch_links}"
        f'<p><a href="{url_for("admin_only")}">Admin-only demo page</a> '
        f"(enforces active_organization.role == admin -- try it after "
        f"switching to an env where your role isn't admin)</p>"
        f'<p><a href="{url_for("logout")}">Log out</a></p>'
        f"<h2>Decoded ID token</h2>"
        f'<pre style="background:#f4f4f4;padding:1em;border-radius:4px;overflow:auto">{pretty_claims}</pre>'
        f"<h2>Decoded access token</h2>"
        f'<pre style="background:#f4f4f4;padding:1em;border-radius:4px;overflow:auto">{pretty_access_claims}</pre>'
    )


@app.route("/admin-only")
@require_role("admin")
def admin_only():
    """Demo protected route -- proves active_organization.role is actually
    enforced somewhere, not just decoded and displayed (see the
    authorization-gap discussion this was built to close). 403s for any
    active org where the caller's role isn't 'admin', re-checked on every
    request against whatever org is active *right now* -- switching to a
    non-admin env and reloading this page is the fastest way to see the
    403 fire.
    """
    claims = current_claims()
    active_org = claims.get("active_organization", {})
    return (
        f"<h1>Admin-only page</h1>"
        f"<p>You're seeing this because your role in the active org "
        f"(<b>{active_org.get('name')}</b>) includes <b>admin</b>.</p>"
        f'<p><a href="{url_for("index")}">Back</a></p>'
    )


@app.route("/switch/<org_name>")
def switch(org_name: str):
    """Switch the session's active organization -- only within the current
    tenant's known env-orgs (MULTI_ENV_TENANTS). This is the app-level
    tenant-isolation check described at MULTI_ENV_TENANTS above: Keycloak's
    switch-organization endpoint itself would allow switching to any org
    the user is a member of, tenant or not.
    """
    tenant = session.get("url_tenant")
    if org_name not in MULTI_ENV_TENANTS.get(tenant, []):
        session["switch_error"] = f"'{org_name}' is not a known environment for tenant '{tenant}'."
        return redirect(url_for("index"))

    try:
        target = next((o for o in my_orgs() if o["name"] == org_name), None)
    except requests.HTTPError:
        session.clear()
        return redirect(url_for("login"))
    if not target:
        session["switch_error"] = f"You are not a member of '{org_name}'."
        return redirect(url_for("index"))

    resp = requests.put(
        f"{KC_INTERNAL_URL}/realms/{PLATFORM_REALM}/users/switch-organization",
        headers={"Authorization": f"Bearer {session['access_token']}"},
        json={"id": target["id"]},
        timeout=10,
    )
    if resp.status_code != 200:
        session["switch_error"] = f"Could not switch to '{org_name}' ({resp.status_code})."
        return redirect(url_for("index"))

    store_tokens(resp.json())
    return redirect(url_for("index"))


def _resolve_support_request(decision: str, endpoint: str):
    if "id_token_raw" not in session:
        return redirect(url_for("login"))
    login_hint = current_claims().get("preferred_username")
    resp = requests.post(f"{CIBA_NOTIFIER_URL}/pending/{login_hint}/{endpoint}", timeout=10)
    if resp.status_code != 200:
        session["switch_error"] = f"Could not {decision} the support request ({resp.status_code})."
    return redirect(url_for("index"))


@app.route("/support-approval/approve")
def support_approve():
    return _resolve_support_request("approve", "approve")


@app.route("/support-approval/deny")
def support_deny():
    return _resolve_support_request("deny", "deny")


@app.route("/login")
def login():
    tenant = current_tenant()
    idp_alias = TENANT_IDP_ALIAS.get(tenant)
    if not idp_alias:
        return f"Unknown tenant '{tenant}' -- no IdP mapping configured.", 404
    redirect_uri = url_for("callback", _external=True)
    return oauth.keycloak.authorize_redirect(redirect_uri, kc_idp_hint=idp_alias)


@app.route("/callback")
def callback():
    """A `/callback` hit whose `state`/`code` doesn't match what's in the
    session (Authlib's MismatchingStateError, a subclass of OAuthError) is
    not necessarily an attack or a real application bug -- it's also exactly
    what a stale/replayed callback URL produces: a browser retry, a
    speculative prefetch of a link firing a second /login before the first
    one's callback lands, or the back button re-submitting an old callback
    after its one-time authorization code was already consumed. All of
    these are transient by nature -- redirecting to a fresh /login recovers
    cleanly, whereas letting this bubble up as an unhandled 500 (confirmed:
    this is what a real "logout, then login" 5xx report traced back to)
    turns a one-off retry into a hard failure the user has to notice and
    manually work around.
    """
    try:
        token = oauth.keycloak.authorize_access_token()
    except OAuthError:
        return redirect(url_for("login"))
    store_tokens(token)
    # Kept separately from id_token_raw (which switch() overwrites on every
    # active-org switch) specifically for /logout's id_token_hint -- see the
    # comment there for why using the *latest* id_token for that breaks
    # logout after a switch.
    session["login_id_token_raw"] = token["id_token"]
    session["url_tenant"] = current_tenant()
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    """Clearing this app's own session cookie is not enough: Keycloak's SSO
    session (and the browser's cookies for it) would still be active, so
    the very next visit to "/" -> "/login" would silently re-authenticate
    via SSO and land the user right back in, looking exactly like logout
    "did nothing". Must also end Keycloak's own session via RP-initiated
    logout (its `end_session` endpoint), not just this app's local session.
    """
    # Deliberately login_id_token_raw here, NOT id_token_raw -- confirmed
    # empirically that keycloak-orgs' switch-organization endpoint mints a
    # token set with a *different* `sid` (session ID) claim than the one the
    # browser's actual Keycloak SSO session is running under (verified by
    # decoding both: the sid changes on every single switch, even though no
    # re-authentication happened). RP-initiated logout matches `id_token_hint`
    # against the real session by its sid -- passing a switch-minted token
    # silently no-ops (Keycloak renders a 200, not the expected redirect
    # chain) instead of ending the session, since it doesn't correspond to
    # any actual active session. This is exactly what "switch org, then
    # logout" reported as "doesn't end session" traced back to.
    # login_id_token_raw is set once at /callback and never touched by
    # switch(), so it always matches the browser's real SSO session
    # regardless of how many times the user has switched orgs since.
    id_token = session.pop("login_id_token_raw", None) or session.pop("id_token_raw", None)
    session.clear()
    params = {"post_logout_redirect_uri": url_for("index", _external=True)}
    if id_token:
        params["id_token_hint"] = id_token
    else:
        params["client_id"] = os.environ["KC_CLIENT_ID"]
    return redirect(f"{KC_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/logout?{urlencode(params)}")


AI_AGENT_CLIENT_ID = "ai-agent"
AI_AGENT_CLIENT_SECRET = "ai-agent-secret-dev-only"
DEMO_APP_CLIENT_ID = "delegation-demo-app"
DEMO_APP_CLIENT_SECRET = "delegation-demo-app-secret-dev-only"


@app.route("/ai-agent")
def ai_agent_index():
    redirect_uri = f"{request.host_url.rstrip('/')}/ai-agent/callback"
    auth_params = {
        "client_id": DEMO_APP_CLIENT_ID,
        "response_type": "code",
        "scope": f"openid profile email delegation:client:{AI_AGENT_CLIENT_ID}",
        "redirect_uri": redirect_uri,
    }
    auth_url = f"{KC_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/auth?{urlencode(auth_params)}"
    return f"""<!doctype html>
<html>
<head>
  <title>AI Agent Token Exchange Delegation Demo</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; color: #24292f; }}
    h1 {{ color: #0969da; border-bottom: 1px solid #d0d7de; padding-bottom: 0.5rem; }}
    .card {{ background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; padding: 1.5rem; margin-bottom: 1.5rem; }}
    .btn {{ display: inline-block; background: #1f883d; color: white; padding: 0.75rem 1.5rem; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 1.1rem; }}
    .btn:hover {{ background: #1a7f37; }}
    code {{ background: #eaeef2; padding: 0.2em 0.4em; border-radius: 3px; font-size: 90%; }}
    ol {{ padding-left: 1.5rem; }}
    li {{ margin-bottom: 0.5rem; }}
  </style>
</head>
<body>
  <h1>AI Agent Token Exchange Delegation (RFC 8693)</h1>
  <div class="card">
    <p>This demo illustrates how an autonomous <b>AI Agent</b> (Non-Human Identity) requests explicit, consent-based permission to act on behalf of a human user in Keycloak 26.8+.</p>
    <h3>How this flow works:</h3>
    <ol>
      <li>Clicking the button below initiates an interactive OAuth 2.0 flow with <code>scope=delegation:client:ai-agent</code>.</li>
      <li>Keycloak prompts the user to grant explicit consent for <code>ai-agent</code> to act on their behalf.</li>
      <li>Keycloak validates Fine-Grained Admin Permissions (FGAP V2) and mints a subject token with a <code>may_act</code> assertion.</li>
      <li>The AI Agent authenticates via client credentials and executes an RFC 8693 token exchange.</li>
      <li>The resulting delegated token contains an immutable <code>act</code> (actor) claim certifying the AI agent while preserving the user's <code>sub</code> identity.</li>
      <li>The agent uses the token to query user-scoped APIs on their behalf.</li>
    </ol>
    <p style="margin-top: 1.5rem;">
      <a class="btn" href="{auth_url}">Launch Interactive Delegation Flow &rarr;</a>
    </p>
    <p style="font-size: 0.9rem; color: #57606a;">Test credentials: <code>shared.user</code> / <code>password123</code> (or <code>alice@acme.example.com</code> / <code>password123</code>)</p>
  </div>
  <p><a href="/">&larr; Back to Demo Index</a></p>
</body>
</html>"""


@app.route("/ai-agent/callback")
def ai_agent_callback():
    error = request.args.get("error")
    error_desc = request.args.get("error_description", "")
    if error:
        return f"""<!doctype html>
<html>
<head><title>Delegation Cancelled</title><style>body{{font-family:system-ui;max-width:700px;margin:3rem auto;color:#24292f;}} .denied{{color:#cf222e;font-size:1.2rem;}}</style></head>
<body>
  <h1>Delegation Request Cancelled or Denied</h1>
  <p class="denied">Error: {error} - {error_desc}</p>
  <p><a href="/ai-agent">&larr; Try Again</a> | <a href="/">Demo Index</a></p>
</body></html>""", 400

    code = request.args.get("code")
    if not code:
        return redirect(url_for("ai_agent_index"))

    redirect_uri = f"{request.host_url.rstrip('/')}/ai-agent/callback"

    # 1. Exchange code for user subject token
    token_resp = requests.post(
        f"{KC_INTERNAL_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/token",
        data={
            "grant_type": "authorization_code",
            "client_id": DEMO_APP_CLIENT_ID,
            "client_secret": DEMO_APP_CLIENT_SECRET,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=10,
    )
    if token_resp.status_code != 200:
        return f"<h3>Failed to exchange authorization code</h3><pre>{token_resp.text}</pre>", 400
    subject_token = token_resp.json()["access_token"]
    subject_claims = decode_jwt_claims(subject_token)

    # 2. Get AI Agent actor token
    actor_resp = requests.post(
        f"{KC_INTERNAL_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": AI_AGENT_CLIENT_ID,
            "client_secret": AI_AGENT_CLIENT_SECRET,
        },
        timeout=10,
    )
    if actor_resp.status_code != 200:
        return f"<h3>Failed to fetch AI agent actor token</h3><pre>{actor_resp.text}</pre>", 400
    actor_token = actor_resp.json()["access_token"]
    actor_claims = decode_jwt_claims(actor_token)

    # 3. Perform RFC 8693 Token Exchange
    exchange_resp = requests.post(
        f"{KC_INTERNAL_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "client_id": AI_AGENT_CLIENT_ID,
            "client_secret": AI_AGENT_CLIENT_SECRET,
            "subject_token": subject_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "actor_token": actor_token,
            "actor_token_type": "urn:ietf:params:oauth:token-type:access_token",
        },
        timeout=10,
    )
    if exchange_resp.status_code != 200:
        return f"<h3>Token Exchange Failed</h3><pre>{exchange_resp.text}</pre>", 400
    delegated_token = exchange_resp.json()["access_token"]
    delegated_claims = decode_jwt_claims(delegated_token)

    # 4. Invoke downstream API with delegated token
    api_resp = requests.get(
        f"{KC_INTERNAL_URL}/realms/{PLATFORM_REALM}/orgs",
        headers={"Authorization": f"Bearer {delegated_token}"},
        timeout=10,
    )
    orgs = api_resp.json() if api_resp.ok else []

    pretty_subject = json.dumps(subject_claims, indent=2)
    pretty_actor = json.dumps(actor_claims, indent=2)
    pretty_delegated = json.dumps(delegated_claims, indent=2)
    pretty_orgs = json.dumps(orgs, indent=2)

    return f"""<!doctype html>
<html>
<head>
  <title>Delegation Result - AI Agent Flow</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #24292f; line-height: 1.5; }}
    h1 {{ color: #1a7f37; }}
    .badge {{ display: inline-block; padding: 0.25em 0.6em; font-size: 85%; font-weight: 600; border-radius: 20px; background: #dafbe1; color: #1a7f37; border: 1px solid #1a7f37; }}
    .step {{ background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; padding: 1.25rem; margin-bottom: 1.5rem; }}
    .step h3 {{ margin-top: 0; color: #0969da; }}
    pre {{ background: #ffffff; border: 1px solid #d0d7de; padding: 1rem; border-radius: 6px; overflow: auto; font-size: 0.9rem; }}
    .highlight {{ background: #fff8c5; padding: 0.1em 0.3em; border-radius: 3px; font-weight: bold; color: #9a6700; }}
  </style>
</head>
<body>
  <h1><span class="badge">&#10003; SUCCESS</span> Delegation Flow Completed via UI</h1>
  <p>User <b>{subject_claims.get('name', subject_claims.get('preferred_username'))}</b> successfully consented to let <b>ai-agent</b> act on their behalf.</p>

  <div class="step">
    <h3>1. User Subject Token (Pre-Exchange)</h3>
    <p>Issued by Keycloak upon user consent. Notice the <span class="highlight">may_act</span> assertion binding the authorized agent and adding it to <span class="highlight">aud</span>:</p>
    <pre>{pretty_subject}</pre>
  </div>

  <div class="step">
    <h3>2. AI Agent Actor Token</h3>
    <p>The AI Agent's non-human identity obtained via service account client credentials:</p>
    <pre>{pretty_actor}</pre>
  </div>

  <div class="step">
    <h3>3. RFC 8693 Delegated Access Token (Post-Exchange)</h3>
    <p>Keycloak verified the authorization and attached the immutable <span class="highlight">act</span> claim. Notice <b>sub</b> is the human user and <b>act</b> identifies the agent:</p>
    <pre>{pretty_delegated}</pre>
  </div>

  <div class="step">
    <h3>4. Downstream API Invocation</h3>
    <p>The AI Agent called <code>GET /realms/platform/orgs</code> using the delegated Bearer token:</p>
    <pre>{pretty_orgs}</pre>
  </div>

  <p>
    <a href="/ai-agent">Run Flow Again &rarr;</a> | 
    <a href="/">Back to Demo Index</a>
  </p>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
