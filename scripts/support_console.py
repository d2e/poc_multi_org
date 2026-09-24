"""Standalone "support agent" tool for the CIBA support-delegation demo (see
docs/ciba-support-delegation.md).

Run as:

    python3 scripts/support_console.py alice@acme.example.com

Plays the support side of the flow end to end:
  1. Starts a real OIDC CIBA backchannel auth request against the
     `support-tool` client (see provision_ciba_support.py) with the given
     email as `login_hint` -- this is what triggers Keycloak to notify
     ciba_notifier.py, which in turn is what makes the "approve/deny"
     banner appear for that user in product-app.
  2. Polls the token endpoint (CIBA grant type) until the user approves,
     denies, or the request expires -- handling `authorization_pending`
     (keep polling) and `slow_down` (back off) per the CIBA/device-flow
     convention, not just the two terminal outcomes.
  3. On approval, calls the user's self-scoped `GET /realms/platform/orgs`
     with the delegated access token -- the exact same call
     `app/product_app.py`'s `my_orgs()` makes -- and prints the result:
     proof the action ran *as the user*, via a token minted only after
     their explicit approval, never via their password or an admin token.

`login_hint` must be a user who already has a `platform`-realm account
(federated users only get one on first login -- see docs/gotchas.md) --
e.g. `alice@acme.example.com`, `alice@globex.example.com`, or `shared.user`.

CIBA's `binding_message` is confirmed (from Keycloak's own validation error,
read out of the actual class file, not assumed) to reject spaces -- kept
short and hyphenated here rather than a free-text sentence.
"""
from __future__ import annotations

import sys
import time

import requests

from keycloak_admin import KC_URL, PLATFORM_REALM

CLIENT_ID = "support-tool"
CLIENT_SECRET = "support-tool-secret-dev-only"
BINDING_MESSAGE = "org-profile-access"
CIBA_GRANT_TYPE = "urn:openid:params:grant-type:ciba"


def request_authorization(login_hint: str) -> dict:
    resp = requests.post(
        f"{KC_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/ext/ciba/auth",
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"scope": "openid", "login_hint": login_hint, "binding_message": BINDING_MESSAGE},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def poll_for_token(auth_req_id: str, interval_s: int, expires_in_s: int) -> dict | None:
    """Returns the token set once approved, or None if denied/expired.
    Mirrors the OAuth2 device-flow polling convention CIBA reuses:
    `authorization_pending` -> keep polling at the current interval,
    `slow_down` -> the server wants us polling less often, back off by 5s.
    """
    deadline = time.monotonic() + expires_in_s
    while time.monotonic() < deadline:
        resp = requests.post(
            f"{KC_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/token",
            auth=(CLIENT_ID, CLIENT_SECRET),
            data={"grant_type": CIBA_GRANT_TYPE, "auth_req_id": auth_req_id},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()

        error = resp.json().get("error")
        if error == "authorization_pending":
            time.sleep(interval_s)
        elif error == "slow_down":
            interval_s += 5
            time.sleep(interval_s)
        else:
            print(f"request denied or failed: {error}")
            return None
    print("request expired waiting for approval")
    return None


def print_org_profile(access_token: str) -> None:
    resp = requests.get(
        f"{KC_URL}/realms/{PLATFORM_REALM}/orgs",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    resp.raise_for_status()
    orgs = resp.json()
    print(f"acting on behalf of the user -- their org memberships ({len(orgs)}):")
    for org in orgs:
        print(f"  - {org['name']} (id={org['id']})")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: python3 {sys.argv[0]} <user-email-or-username>")
    login_hint = sys.argv[1]

    print(f"requesting authorization from '{login_hint}' ...")
    auth = request_authorization(login_hint)
    print(f"waiting for approval (auth_req_id issued, expires in {auth['expires_in']}s) ...")

    token = poll_for_token(auth["auth_req_id"], auth["interval"], auth["expires_in"])
    if not token:
        raise SystemExit(1)

    print("approved -- received a delegated access token")
    print_org_profile(token["access_token"])


if __name__ == "__main__":
    main()
