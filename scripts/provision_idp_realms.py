"""Creates one "federated auth" realm per tenant in TENANTS (org1-idp, org2-idp,
org3-idp, acme-idp). Each simulates that tenant's own IdP: a confidential
broker client + one test employee user.
"""
from __future__ import annotations

from keycloak_admin import AdminClient, KC_URL, PLATFORM_REALM, TENANTS, admin_token


def main() -> None:
    admin = AdminClient(token=admin_token())

    for tenant in TENANTS:
        org = tenant["org"]
        realm = f"{org}-idp"
        secret = f"brokersecret-{org}"
        redirect = f"{KC_URL}/realms/{PLATFORM_REALM}/broker/{org}/endpoint"

        if admin.exists(f"/admin/realms/{realm}"):
            print(f"realm {realm} already exists, skipping")
            continue

        admin.post("/admin/realms", json={"realm": realm, "enabled": True, "sslRequired": "none"})
        print(f"created realm {realm}")

        admin.post(
            f"/admin/realms/{realm}/clients",
            json={
                "clientId": "platform-broker",
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": False,
                "standardFlowEnabled": True,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": False,
                "redirectUris": [redirect],
                "secret": secret,
                # Without this, RP-initiated logout that propagates up to
                # this IdP (federated logout, e.g. product-app's /logout ->
                # platform's end_session -> this realm's end_session) fails
                # with "Invalid redirect uri" at *this* client -- an unset
                # post.logout.redirect.uris rejects all post-logout
                # redirects by default, it doesn't fall back to redirectUris.
                "attributes": {"post.logout.redirect.uris": f"{redirect}/*"},
            },
        )
        print(f"created broker client in {realm}")

        user = f"alice@{tenant['domain']}"
        admin.post(
            f"/admin/realms/{realm}/users",
            json={
                "username": user,
                "email": user,
                "emailVerified": True,
                "enabled": True,
                "firstName": "Alice",
                "lastName": tenant["display"],
                "credentials": [{"type": "password", "value": "password123", "temporary": False}],
            },
        )
        print(f"created test user {user} in {realm} (password: password123)")


if __name__ == "__main__":
    main()
