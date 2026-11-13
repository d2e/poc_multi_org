"""Automated smoke test:
 1. Drives a real browser-style login for alice@org3.example.com through the
    Home IdP Discovery -> org3-idp -> broker callback chain, confirms the
    final token carries active_organization.name == "org3".
 2. Logs in shared.user (member of org1 + org2), confirms default active org,
    then calls switch-organization and confirms the claim updates to org2.
 3. Drives the subdomain-based IdP routing demo (docs/tenant-routing.md):
    hitting the product app's acme subdomain must land directly on
    acme-idp's login page (kc_idp_hint, no email prompt), and after login
    the app must show the active_organization claim as "acme".
 4. Drives the multi-environment tenant demo (docs/multi-env-tenants.md):
    logs in as alice@globex.example.com (globex-dev, role admin), switches
    to globex-stage (role developer) then globex-prod (role viewer), and
    confirms an attempted switch to a different tenant's org (org1) is
    rejected rather than silently allowed.
"""
from __future__ import annotations

import base64
import json
import re
from urllib.parse import urlparse

import requests

import state
from browser_login import PlainCookieJarSession, form_action, tenant_get
from keycloak_admin import KC_URL, PLATFORM_REALM


def pass_(msg: str) -> None:
    print(f"PASS: {msg}")


def fail(msg: str) -> None:
    raise SystemExit(f"FAIL: {msg}")


def decode_claim(access_token: str, claim: str) -> str | None:
    payload = access_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    for part in claim.split("."):
        claims = claims.get(part) if isinstance(claims, dict) else None
    return claims


def sso_session_ended(session: PlainCookieJarSession, product_app_url: str, tenant_host: str) -> bool:
    """Hits /login again and follows the redirect chain, returning True only
    if it's forced to show a real password form somewhere along the way
    (proof Keycloak's SSO session actually ended), False if it silently
    re-authenticates and lands back at the app without ever asking for
    credentials.

    This is deliberately NOT "does GET / return 200 with 'Welcome' on it" --
    that can never distinguish the two cases: the app's own local session is
    always cleared by /logout regardless of whether Keycloak's SSO session
    did, so GET / always 302s to /login either way. The only real signal is
    what /login's own redirect chain does next.
    """
    loc = tenant_get(session, f"{product_app_url}/login", tenant_host, product_app_url, allow_redirects=False).headers.get(
        "Location"
    )
    for _ in range(8):
        if not loc:
            return False
        if loc.startswith("/"):
            loc = f"http://{tenant_host}{loc}"
        resp = (
            tenant_get(session, loc, tenant_host, product_app_url, allow_redirects=False)
            if tenant_host in loc
            else session.get(loc, allow_redirects=False)
        )
        if resp.status_code == 200 and 'name="password"' in resp.text:
            return True
        if tenant_host in loc and resp.status_code == 200:
            # Landed back on the app itself without ever hitting a password
            # form -- silent re-auth via a still-alive SSO session.
            return False
        loc = resp.headers.get("Location")
    return False


def at_redirect_uri(location: str) -> bool:
    """True once a redirect chain lands back at redirect_uri's root (KC_URL/),
    as opposed to an intermediate broker-callback URL that happens to share
    the same host (e.g. .../realms/platform/broker/org3/endpoint?code=...).
    """
    parsed, base = urlparse(location), urlparse(KC_URL)
    return (parsed.scheme, parsed.netloc) == (base.scheme, base.netloc) and parsed.path in ("", "/")


def test_federated_login() -> None:
    print("--- Test 1: federated login via Home IdP Discovery (org3) ---")
    session = PlainCookieJarSession()

    auth_page = session.get(
        f"{KC_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/auth",
        params={"client_id": "poc-app", "response_type": "code", "redirect_uri": f"{KC_URL}/", "scope": "openid"},
    )
    resp = session.post(form_action(auth_page.text), data={"username": "alice@org3.example.com"}, allow_redirects=False)
    location = resp.headers.get("Location")
    if not location:
        fail("Home IdP Discovery did not redirect to org3-idp")
    if "org3-idp" not in location:
        fail(f"redirected somewhere unexpected: {location}")

    page2 = session.get(location)
    resp2 = session.post(
        form_action(page2.text),
        data={"username": "alice@org3.example.com", "password": "password123"},
        allow_redirects=False,
    )
    location = resp2.headers.get("Location")

    # follow the broker/first-broker-login/post-broker-login chain until we land
    # back at redirect_uri -- do NOT stop early just because an intermediate
    # hop's URL happens to contain "code=" (e.g. the broker callback URL
    # carries org3-idp's authorization code, which still needs Keycloak to
    # process server-side before it issues platform's own code).
    for _ in range(6):
        if not location or at_redirect_uri(location):
            break
        location = session.get(location, allow_redirects=False).headers.get("Location")

    if not location or "code=" not in location:
        fail(f"did not complete broker login (final: {location})")
    code = re.search(r"code=([^&]*)", location).group(1)

    token = session.post(
        f"{KC_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/token",
        data={"grant_type": "authorization_code", "client_id": "poc-app", "code": code, "redirect_uri": f"{KC_URL}/"},
    ).json()
    org_name = decode_claim(token["access_token"], "active_organization.name")
    if org_name != "org3":
        fail(f"expected active_organization.name=org3, got {org_name}")
    pass_("alice@org3.example.com federated in via org3-idp, active_organization=org3")


def test_active_org_switch() -> None:
    print("--- Test 2: active-organization switch (shared.user) ---")
    token = requests.post(
        f"{KC_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/token",
        data={"client_id": "poc-app", "username": "shared.user", "password": "password123", "grant_type": "password"},
    ).json()
    default_org = decode_claim(token["access_token"], "active_organization.name")
    print(f"default active organization: {default_org}")

    org2 = state.load("org2.id")
    switch_resp = requests.put(
        f"{KC_URL}/realms/{PLATFORM_REALM}/users/switch-organization",
        headers={"Authorization": f"Bearer {token['access_token']}"},
        json={"id": org2},
    ).json()
    new_org = decode_claim(switch_resp["access_token"], "active_organization.name")
    if new_org != "org2":
        fail(f"expected switched active_organization.name=org2, got {new_org}")
    pass_("switch-organization moved shared.user's active org to org2")


PRODUCT_APP_URL = "http://127.0.0.1:3000"
TENANT_HOST = "acme.sample.127.0.0.1.nip.io:3000"


def _tenant_get(session: PlainCookieJarSession, url: str, **kw) -> requests.Response:
    return tenant_get(session, url, TENANT_HOST, PRODUCT_APP_URL, **kw)


def test_subdomain_idp_routing() -> None:
    print("--- Test 3: subdomain-based IdP routing (acme, kc_idp_hint) ---")
    session = PlainCookieJarSession()

    login_resp = _tenant_get(session, f"{PRODUCT_APP_URL}/login", allow_redirects=False)
    kc_auth_url = login_resp.headers.get("Location")
    if not kc_auth_url or "kc_idp_hint=acme" not in kc_auth_url:
        fail(f"product app did not send kc_idp_hint=acme: {kc_auth_url}")

    # Must land directly on acme-idp's login page -- never on platform's
    # Home IdP Discovery email-prompt page. That's the actual proof
    # kc_idp_hint (not email-domain matching) drove this.
    hop = session.get(kc_auth_url, allow_redirects=False)
    hop2_url = hop.headers.get("Location")
    if not hop2_url or "/broker/acme/login" not in hop2_url:
        fail(f"kc_idp_hint did not route straight to acme's IdP: {hop2_url}")
    idp_login_url = session.get(hop2_url, allow_redirects=False).headers.get("Location")
    if not idp_login_url or "realms/acme-idp" not in idp_login_url:
        fail(f"expected redirect to realms/acme-idp, got: {idp_login_url}")

    login_page = session.get(idp_login_url)
    resp = session.post(
        form_action(login_page.text),
        data={"username": "alice@acme.example.com", "password": "password123"},
        allow_redirects=False,
    )
    location = resp.headers.get("Location")
    for _ in range(6):
        if not location or TENANT_HOST in location:
            break
        location = session.get(location, allow_redirects=False).headers.get("Location")
    if not location or TENANT_HOST not in location:
        fail(f"did not complete broker login back to product-app (final: {location})")

    callback_resp = _tenant_get(session, location, allow_redirects=False)
    if callback_resp.status_code != 302:
        fail(f"product app /callback failed: {callback_resp.status_code}")

    index_page = _tenant_get(session, f"{PRODUCT_APP_URL}/")
    if "alice@acme.example.com" not in index_page.text:
        fail(f"product app did not show the logged-in user: {index_page.text[:200]}")
    # Look for the rendered summary line specifically, not just anywhere on
    # the page -- the page also dumps the raw decoded token JSON, which
    # contains the substring "active_organization" too.
    if "post-login): <b>acme</b>" not in index_page.text:
        fail(f"product app's active_organization claim was not 'acme': {index_page.text}")
    pass_("subdomain routing landed on acme-idp with no email prompt, active_organization=acme")

    # Logout must end Keycloak's own SSO session too, not just this app's
    # local one -- otherwise revisiting "/" silently re-authenticates via
    # SSO and looks exactly like logout did nothing (see gotchas.md).
    logout_resp = _tenant_get(session, f"{PRODUCT_APP_URL}/logout", allow_redirects=False)
    loc = logout_resp.headers.get("Location")
    for _ in range(6):
        if not loc:
            break
        if loc.startswith("/"):
            loc = f"http://{TENANT_HOST}{loc}"
        r = _tenant_get(session, loc, allow_redirects=False) if TENANT_HOST in loc else session.get(loc, allow_redirects=False)
        loc = r.headers.get("Location")
    if not sso_session_ended(session, PRODUCT_APP_URL, TENANT_HOST):
        fail("logout did not end the Keycloak SSO session -- silently re-authenticated")
    pass_("logout ended the Keycloak SSO session (no silent re-auth)")


GLOBEX_TENANT_HOST = "globex.sample.127.0.0.1.nip.io:3000"


def _globex_get(session: PlainCookieJarSession, url: str, **kw) -> requests.Response:
    return tenant_get(session, url, GLOBEX_TENANT_HOST, PRODUCT_APP_URL, **kw)


def _active_org_and_role(page_html: str) -> tuple[str | None, str | None]:
    m = re.search(r"active_organization claim.*?<b>(.*?)</b>, role\(s\): <b>(.*?)</b>", page_html)
    return (m.group(1), m.group(2)) if m else (None, None)


def test_multi_env_tenant_switching() -> None:
    print("--- Test 4: multi-environment tenant switching (globex) ---")
    session = PlainCookieJarSession()

    login_resp = _globex_get(session, f"{PRODUCT_APP_URL}/login", allow_redirects=False)
    kc_auth_url = login_resp.headers.get("Location")
    if not kc_auth_url or "kc_idp_hint=globex-dev" not in kc_auth_url:
        fail(f"product app did not send kc_idp_hint=globex-dev: {kc_auth_url}")

    hop = session.get(kc_auth_url, allow_redirects=False)
    idp_login_url = session.get(hop.headers.get("Location"), allow_redirects=False).headers.get("Location")
    if not idp_login_url or "realms/globex-idp" not in idp_login_url:
        fail(f"expected redirect to realms/globex-idp, got: {idp_login_url}")

    login_page = session.get(idp_login_url)
    resp = session.post(
        form_action(login_page.text),
        data={"username": "alice@globex.example.com", "password": "password123"},
        allow_redirects=False,
    )
    location = resp.headers.get("Location")
    for _ in range(6):
        if not location or GLOBEX_TENANT_HOST in location:
            break
        location = session.get(location, allow_redirects=False).headers.get("Location")
    if not location or GLOBEX_TENANT_HOST not in location:
        fail(f"did not complete broker login back to product-app (final: {location})")
    _globex_get(session, location, allow_redirects=False)

    pass_("logged into globex via kc_idp_hint, no email prompt")

    # Active org is a persisted user attribute in Keycloak (see
    # docs/multi-env-tenants.md) -- a login restores whatever was active
    # last time, it does not always default to the "primary" env. So force
    # a known starting point (globex-dev) rather than asserting on
    # whatever this fresh login happened to restore.
    _globex_get(session, f"{PRODUCT_APP_URL}/switch/globex-dev", allow_redirects=False)
    org, role = _active_org_and_role(_globex_get(session, f"{PRODUCT_APP_URL}/").text)
    if (org, role) != ("globex-dev", "admin"):
        fail(f"expected globex-dev/admin, got {org}/{role}")
    pass_("active org is globex-dev with role admin")

    _globex_get(session, f"{PRODUCT_APP_URL}/switch/globex-stage", allow_redirects=False)
    org, role = _active_org_and_role(_globex_get(session, f"{PRODUCT_APP_URL}/").text)
    if (org, role) != ("globex-stage", "developer"):
        fail(f"expected globex-stage/developer after switch, got {org}/{role}")
    pass_("switched to globex-stage with role developer")

    _globex_get(session, f"{PRODUCT_APP_URL}/switch/globex-prod", allow_redirects=False)
    org, role = _active_org_and_role(_globex_get(session, f"{PRODUCT_APP_URL}/").text)
    if (org, role) != ("globex-prod", "viewer"):
        fail(f"expected globex-prod/viewer after switch, got {org}/{role}")
    pass_("switched to globex-prod with role viewer")

    # Cross-tenant: org1 belongs to a different tenant entirely, and alice
    # isn't a member of it anyway -- must be rejected, not silently allowed.
    _globex_get(session, f"{PRODUCT_APP_URL}/switch/org1", allow_redirects=False)
    page = _globex_get(session, f"{PRODUCT_APP_URL}/").text
    org, role = _active_org_and_role(page)
    if org != "globex-prod" or "not a known environment" not in page:
        fail(f"cross-tenant switch to org1 was not rejected as expected: {page[:300]}")
    pass_("cross-tenant switch to org1 was rejected, active org unchanged")

    # Authorization enforcement: /admin-only requires active_organization.role
    # == "admin", re-checked against whatever org is active *right now* (not
    # a role captured at login time). Confirms the role claim is actually
    # enforced somewhere, not just decoded and displayed -- see gotchas.md.
    _globex_get(session, f"{PRODUCT_APP_URL}/switch/globex-dev", allow_redirects=False)
    resp = _globex_get(session, f"{PRODUCT_APP_URL}/admin-only")
    if resp.status_code != 200:
        fail(f"expected 200 for admin role on globex-dev, got {resp.status_code}")
    pass_("admin-only route allows access when active org role is admin")

    _globex_get(session, f"{PRODUCT_APP_URL}/switch/globex-prod", allow_redirects=False)
    resp = _globex_get(session, f"{PRODUCT_APP_URL}/admin-only")
    if resp.status_code != 403:
        fail(f"expected 403 for viewer role on globex-prod, got {resp.status_code}")
    pass_("admin-only route rejects access when active org role is viewer (403)")

    # Logout *after* having switched orgs at least once -- deliberately not
    # just "logout after a fresh login" (already covered by the acme test
    # above). keycloak-orgs' switch-organization endpoint mints a token set
    # with a different `sid` (session ID) than the one the browser's actual
    # SSO session runs under; using that latest token as /logout's
    # id_token_hint silently no-ops instead of ending the real session (see
    # gotchas.md). This regression-tests the fix (a separate
    # login_id_token_raw pinned at /callback, never overwritten by switch()).
    logout_resp = _globex_get(session, f"{PRODUCT_APP_URL}/logout", allow_redirects=False)
    loc = logout_resp.headers.get("Location")
    for _ in range(6):
        if not loc:
            break
        if loc.startswith("/"):
            loc = f"http://{GLOBEX_TENANT_HOST}{loc}"
        r = _globex_get(session, loc, allow_redirects=False) if GLOBEX_TENANT_HOST in loc else session.get(loc, allow_redirects=False)
        loc = r.headers.get("Location")
    if not sso_session_ended(session, PRODUCT_APP_URL, GLOBEX_TENANT_HOST):
        fail("logout after switching orgs did not end the Keycloak SSO session -- silently re-authenticated")
    pass_("logout after switching orgs ended the Keycloak SSO session (no silent re-auth)")


def test_ai_agent_delegation() -> None:
    print("--- Test 5: AI agent token exchange delegation (RFC 8693 'act' claim) ---")
    import demo_ai_agent_delegation

    res = demo_ai_agent_delegation.run_delegation_flow("shared.user", "password123")
    act = res["delegated_claims"].get("act")
    if not act or act.get("client_id") != "ai-agent":
        fail(f"expected delegated token to carry act.client_id == 'ai-agent', got {act}")
    pass_("AI agent token exchange delegation issued 'act' claim asserting client_id=ai-agent")

    if res["delegated_claims"].get("sub") != res["subject_claims"].get("sub"):
        fail("delegated token subject did not match original human user")
    pass_("delegated token subject preserved the original human user identity")

    if not res.get("orgs"):
        fail("AI agent failed to query organizations with delegated token")
    pass_("AI agent successfully invoked downstream user-scoped API via delegated token")


def main() -> None:
    test_federated_login()
    test_active_org_switch()
    test_subdomain_idp_routing()
    test_multi_env_tenant_switching()
    test_ai_agent_delegation()
    print()
    print("All checks passed.")


if __name__ == "__main__":
    main()
