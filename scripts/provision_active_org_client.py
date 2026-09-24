"""Creates a public test client, an "active-organization" client scope with the
oidc-active-organization-mapper, and assigns it to the client by default.
"""
from __future__ import annotations

import state
from keycloak_admin import AdminClient, PLATFORM_REALM, admin_token


def main() -> None:
    admin = AdminClient(token=admin_token())

    if state.has("poc-app.uuid"):
        print("poc-app client already provisioned, skipping")
        return

    resp = admin.post(
        f"/admin/realms/{PLATFORM_REALM}/clients",
        json={
            "clientId": "poc-app",
            "publicClient": True,
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": True,
            "redirectUris": ["http://localhost:8082/*", "http://localhost:3000/*"],
            "webOrigins": ["*"],
        },
    )
    client_uuid = AdminClient.location_id(resp)
    state.save("poc-app.uuid", client_uuid)
    print(f"created public client poc-app ({client_uuid})")

    scope_resp = admin.post(
        f"/admin/realms/{PLATFORM_REALM}/client-scopes",
        json={
            "name": "active-organization",
            "protocol": "openid-connect",
            "attributes": {"include.in.token.scope": "true", "display.on.consent.screen": "false"},
        },
    )
    scope_id = AdminClient.location_id(scope_resp)
    state.save("active-organization-scope.id", scope_id)
    print(f"created client scope active-organization ({scope_id})")

    admin.post(
        f"/admin/realms/{PLATFORM_REALM}/client-scopes/{scope_id}/protocol-mappers/models",
        json={
            "name": "active-organization-mapper",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-active-organization-mapper",
            "config": {
                "included.active.organization.properties": "id, name, role, attribute",
                "claim.name": "active_organization",
                "jsonType.label": "JSON",
                "access.token.claim": "true",
                "id.token.claim": "true",
                "userinfo.token.claim": "true",
            },
        },
    )
    print("added active-organization protocol mapper")

    admin.put(f"/admin/realms/{PLATFORM_REALM}/clients/{client_uuid}/default-client-scopes/{scope_id}")
    print("assigned active-organization scope to poc-app as default")


if __name__ == "__main__":
    main()
