"""Provisions the `support-tool` client for the CIBA support-delegation demo
(see docs/ciba-support-delegation.md) -- a confidential client in `platform`
that starts a real OIDC CIBA (Client Initiated Backchannel Authentication)
request, asking a specific end user to approve support acting on their
behalf, then polls for a user-scoped token once they do.

Client attributes below (`oidc.ciba.grant.enabled` / `backchannel.token.
delivery.mode`) are not documented anywhere consistent across Keycloak
versions -- confirmed empirically against this project's actual running
phasetwo-keycloak:26.6.6 by creating a throwaway client with these exact
attributes and firing a real backchannel auth request against it (got a
real `auth_req_id` back, not `unauthorized_client`). See
docs/ciba-support-delegation.md for the full spike writeup.

The realm's CIBA policy (poll mode, 120s expiry, 5s interval, login_hint
user resolution) is already Keycloak's out-of-the-box default on this
image -- confirmed by reading `platform`'s own realm representation back
before this script ever ran (`cibaBackchannelTokenDeliveryMode: "poll"`,
etc. were already present, unset by any script here). Set explicitly below
anyway rather than relied on implicitly, so this stays correct even if a
future realm change or Keycloak upgrade ever moves those defaults.
"""
from __future__ import annotations

import state
from keycloak_admin import AdminClient, PLATFORM_REALM, admin_token

CLIENT_ID = "support-tool"
CLIENT_SECRET = "support-tool-secret-dev-only"

CIBA_REALM_ATTRIBUTES = {
    "cibaBackchannelTokenDeliveryMode": "poll",
    "cibaExpiresIn": "120",
    "cibaInterval": "5",
    "cibaAuthRequestedUserHint": "login_hint",
}


def main() -> None:
    admin = AdminClient(token=admin_token())

    realm = admin.get(f"/admin/realms/{PLATFORM_REALM}").json()
    realm["attributes"] = {**realm.get("attributes", {}), **CIBA_REALM_ATTRIBUTES}
    admin.put(f"/admin/realms/{PLATFORM_REALM}", json=realm)
    print(f"{PLATFORM_REALM} realm CIBA policy set (poll, 120s expiry, 5s interval, login_hint)")

    if state.has("support-tool.uuid"):
        print("support-tool client already provisioned, skipping")
        return

    resp = admin.post(
        f"/admin/realms/{PLATFORM_REALM}/clients",
        json={
            "clientId": CLIENT_ID,
            "publicClient": False,
            "secret": CLIENT_SECRET,
            "standardFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": False,
            "attributes": {
                "oidc.ciba.grant.enabled": "true",
                "backchannel.token.delivery.mode": "poll",
            },
        },
    )
    client_uuid = AdminClient.location_id(resp)
    state.save("support-tool.uuid", client_uuid)
    print(f"created confidential client {CLIENT_ID} ({client_uuid}) in {PLATFORM_REALM}, CIBA grant enabled (poll)")


if __name__ == "__main__":
    main()
