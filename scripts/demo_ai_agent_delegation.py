"""Demonstration and verification script for AI Agent Token Exchange Delegation (Keycloak 26.8+).

Reference: https://skycloak.io/blog/keycloak-26-8-token-exchange-delegation-ai-agents/

Executes the full RFC 8693 token exchange delegation workflow:
1. User authenticates via interactive OAuth2 Authorization Code flow with scope:
   `openid profile email delegation:client:ai-agent`.
2. User explicitly consents to let `ai-agent` act on their behalf.
3. User receives a subject token containing the `may_act` claim and audience.
4. AI Agent obtains its own actor token via `client_credentials`.
5. AI Agent executes Token Exchange (`urn:ietf:params:oauth:grant-type:token-exchange`).
6. Keycloak validates the `may_act` permission and FGAP V2 authorization.
7. Keycloak mints a short-lived delegated token with an `act` (actor) claim:
   `"act": {"client_id": "ai-agent", "sub": "<service-account-uuid>"}`.
8. AI Agent calls downstream user-scoped API (`/realms/platform/orgs`) using
   the delegated token.

Usage:
    python3 scripts/demo_ai_agent_delegation.py [username] [password]
"""
from __future__ import annotations

import base64
import json
import sys
import urllib.parse
from typing import Any

import requests

from browser_login import PlainCookieJarSession, form_action
from keycloak_admin import KC_URL, PLATFORM_REALM

AI_AGENT_CLIENT_ID = "ai-agent"
AI_AGENT_CLIENT_SECRET = "ai-agent-secret-dev-only"

DEMO_APP_CLIENT_ID = "delegation-demo-app"
DEMO_APP_CLIENT_SECRET = "delegation-demo-app-secret-dev-only"
REDIRECT_URI = "http://localhost:3000/callback"


def decode_jwt(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def login_and_authorize_delegation(username: str, password: str) -> str:
    """Performs the browser login and grants consent for delegation:client:ai-agent,
    returning the authorization code."""
    s = PlainCookieJarSession()
    auth_url = (
        f"{KC_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/auth?"
        + urllib.parse.urlencode({
            "client_id": DEMO_APP_CLIENT_ID,
            "response_type": "code",
            "scope": f"openid profile email delegation:client:{AI_AGENT_CLIENT_ID}",
            "redirect_uri": REDIRECT_URI,
        })
    )

    # Step 1: GET auth endpoint
    resp = s.get(auth_url)
    resp.raise_for_status()

    # Step 2: Username
    step2 = s.post(form_action(resp.text), data={"username": username}, allow_redirects=False)

    # Step 3: Password
    step3 = s.post(form_action(step2.text), data={"password": password}, allow_redirects=False)

    loc = step3.headers.get("Location")
    if not loc:
        raise RuntimeError(f"Login failed at password step: {step3.status_code} {step3.text[:300]}")

    # Check if redirected to consent page
    if "execution=OAUTH_GRANT" in loc or "login-actions/consent" in loc:
        consent_page = s.get(loc)
        consent_action = form_action(consent_page.text)
        if consent_action.startswith("/"):
            consent_action = f"{KC_URL}{consent_action}"
        grant_resp = s.post(consent_action, data={"accept": "Yes"}, allow_redirects=False)
        loc = grant_resp.headers.get("Location")

    if not loc or "code=" not in loc:
        raise RuntimeError(f"Failed to obtain authorization code: {loc}")

    code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["code"][0]
    return code


def exchange_code_for_subject_token(code: str) -> str:
    """Exchanges authorization code for user's subject access token."""
    resp = requests.post(
        f"{KC_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/token",
        data={
            "grant_type": "authorization_code",
            "client_id": DEMO_APP_CLIENT_ID,
            "client_secret": DEMO_APP_CLIENT_SECRET,
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_ai_agent_actor_token() -> str:
    """Obtains the AI Agent's service account actor token via client credentials."""
    resp = requests.post(
        f"{KC_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": AI_AGENT_CLIENT_ID,
            "client_secret": AI_AGENT_CLIENT_SECRET,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def perform_token_exchange(subject_token: str, actor_token: str) -> dict[str, Any]:
    """Exchanges user subject_token + AI agent actor_token for a delegated token."""
    resp = requests.post(
        f"{KC_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/token",
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
    resp.raise_for_status()
    return resp.json()


def call_user_api_with_delegated_token(delegated_token: str) -> list[dict]:
    """Invokes self-scoped user API to prove delegated access."""
    resp = requests.get(
        f"{KC_URL}/realms/{PLATFORM_REALM}/orgs",
        headers={"Authorization": f"Bearer {delegated_token}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def run_delegation_flow(username: str = "shared.user", password: str = "password123") -> dict[str, Any]:
    print("=" * 64)
    print(f"AI Agent Token Exchange Delegation Workflow (Keycloak 26.8+)")
    print(f"Target User: {username}")
    print(f"AI Agent Client: {AI_AGENT_CLIENT_ID}")
    print("=" * 64)

    # 1. User login and consent
    print("\n[Step 1] Interactive User Login & Delegation Consent...")
    code = login_and_authorize_delegation(username, password)
    print("  -> User granted consent for scope: delegation:client:ai-agent")

    # 2. User subject token
    print("\n[Step 2] Obtaining User Subject Token...")
    subject_token = exchange_code_for_subject_token(code)
    subject_claims = decode_jwt(subject_token)
    print(f"  -> User subject (sub): {subject_claims.get('sub')}")
    print(f"  -> User preferred_username: {subject_claims.get('preferred_username')}")
    print(f"  -> Token aud: {subject_claims.get('aud')}")
    print(f"  -> may_act claim: {json.dumps(subject_claims.get('may_act'))}")

    if not subject_claims.get("may_act"):
        raise ValueError("Subject token is missing expected 'may_act' claim!")
    if subject_claims.get("may_act", {}).get("client_id") != AI_AGENT_CLIENT_ID:
        raise ValueError(f"may_act client_id mismatch: {subject_claims.get('may_act')}")

    # 3. AI Agent actor token
    print("\n[Step 3] AI Agent Obtains Service Account Actor Token...")
    actor_token = get_ai_agent_actor_token()
    actor_claims = decode_jwt(actor_token)
    print(f"  -> Actor sub: {actor_claims.get('sub')}")
    print(f"  -> Actor client (azp): {actor_claims.get('azp')}")

    # 4. Token Exchange Delegation (RFC 8693)
    print("\n[Step 4] Executing RFC 8693 Token Exchange Delegation...")
    token_resp = perform_token_exchange(subject_token, actor_token)
    delegated_token = token_resp["access_token"]
    delegated_claims = decode_jwt(delegated_token)

    print("  -> Exchange successful (HTTP 200)")
    print(f"  -> Delegated sub (End User): {delegated_claims.get('sub')}")
    print(f"  -> Delegated azp (Caller): {delegated_claims.get('azp')}")
    print(f"  -> Scopes granted: {delegated_claims.get('scope')}")
    print(f"  -> *** DELEGATION CHAIN (act claim) ***:")
    print(f"     {json.dumps(delegated_claims.get('act'), indent=6)}")

    if not delegated_claims.get("act"):
        raise ValueError("Delegated token is missing expected 'act' claim!")
    if delegated_claims.get("act", {}).get("client_id") != AI_AGENT_CLIENT_ID:
        raise ValueError(f"act client_id mismatch: {delegated_claims.get('act')}")
    if delegated_claims.get("sub") != subject_claims.get("sub"):
        raise ValueError("Delegated sub does not match original user subject!")

    # 5. Call Downstream API
    print("\n[Step 5] AI Agent Invoking Downstream API with Delegated Token...")
    orgs = call_user_api_with_delegated_token(delegated_token)
    print(f"  -> Successfully queried user's organizations ({len(orgs)} memberships):")
    for org in orgs:
        print(f"     * {org['name']} (id={org['id']})")

    print("\n" + "=" * 64)
    print("SUCCESS: Token Exchange Delegation workflow completed perfectly.")
    print("=" * 64)

    return {
        "subject_claims": subject_claims,
        "delegated_claims": delegated_claims,
        "orgs": orgs,
    }


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else "shared.user"
    p = sys.argv[2] if len(sys.argv) > 2 else "password123"
    run_delegation_flow(u, p)
