"""Creates a shared local user in the "platform" realm and makes them a member of
org1 AND org2, purely to demonstrate the active-organization switch endpoint
(a real deployment's members would arrive via the federated IdPs instead).
"""
from __future__ import annotations

import state
from keycloak_admin import AdminClient, PLATFORM_REALM, admin_token


def main() -> None:
    admin = AdminClient(token=admin_token())

    if state.has("shared.user.id"):
        print("shared.user already provisioned, skipping")
        return

    resp = admin.post(
        f"/admin/realms/{PLATFORM_REALM}/users",
        json={
            "username": "shared.user",
            "email": "shared.user@example.com",
            "emailVerified": True,
            "enabled": True,
            "firstName": "Shared",
            "lastName": "User",
            "credentials": [{"type": "password", "value": "password123", "temporary": False}],
        },
    )
    user_id = AdminClient.location_id(resp)
    state.save("shared.user.id", user_id)
    print(f"created shared.user ({user_id}), password: password123")

    org1 = state.load("org1.id")
    org2 = state.load("org2.id")
    admin.put(f"/realms/{PLATFORM_REALM}/orgs/{org1}/members/{user_id}")
    admin.put(f"/realms/{PLATFORM_REALM}/orgs/{org2}/members/{user_id}")
    print("added shared.user to org1 and org2")


if __name__ == "__main__":
    main()
