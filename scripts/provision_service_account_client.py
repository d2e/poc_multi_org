"""Creates a confidential "automation-svc" client in the master realm with a
service account, for headless service-to-service calls against the platform
realm's Admin REST API -- an alternative to driving a real browser/user login
(or the master admin username+password) just to make an API call.

Scoped to the platform realm only: the service account is granted every
client role from master's auto-generated `platform-realm` client (the same
role set the admin console's "platform" realm-admin assignment uses), not
global master-admin rights.

Fixed dev secret, same pattern as product-app/org broker clients -- not for
production use, just reproducible across `docker compose down -v`.
"""
from __future__ import annotations

import state
from keycloak_admin import AdminClient, admin_token

MASTER_REALM = "master"
CLIENT_ID = "automation-svc"
CLIENT_SECRET = "automation-svc-secret-dev-only"
TARGET_REALM_CLIENT = "platform-realm"


def main() -> None:
    admin = AdminClient(token=admin_token())

    if state.has("automation-svc.uuid"):
        print("automation-svc client already provisioned, skipping")
        return

    resp = admin.post(
        f"/admin/realms/{MASTER_REALM}/clients",
        json={
            "clientId": CLIENT_ID,
            "publicClient": False,
            "secret": CLIENT_SECRET,
            "serviceAccountsEnabled": True,
            "standardFlowEnabled": False,
            "directAccessGrantsEnabled": False,
        },
    )
    client_uuid = AdminClient.location_id(resp)
    state.save("automation-svc.uuid", client_uuid)
    print(f"created confidential client {CLIENT_ID} ({client_uuid}) in {MASTER_REALM}")

    sa_user = admin.get(f"/admin/realms/{MASTER_REALM}/clients/{client_uuid}/service-account-user").json()
    sa_user_id = sa_user["id"]

    platform_realm_client = admin.get(
        f"/admin/realms/{MASTER_REALM}/clients", params={"clientId": TARGET_REALM_CLIENT}
    ).json()[0]
    platform_realm_client_uuid = platform_realm_client["id"]

    roles = admin.get(f"/admin/realms/{MASTER_REALM}/clients/{platform_realm_client_uuid}/roles").json()
    admin.post(
        f"/admin/realms/{MASTER_REALM}/users/{sa_user_id}/role-mappings/clients/{platform_realm_client_uuid}",
        json=roles,
    )
    print(f"granted {len(roles)} '{TARGET_REALM_CLIENT}' roles to {CLIENT_ID}'s service account")


if __name__ == "__main__":
    main()
