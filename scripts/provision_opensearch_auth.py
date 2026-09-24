"""Provisions Keycloak-side identity for OpenSearch/OpenSearch Dashboards
login (see docs/opensearch-auth.md): a realm role, a client scope + a
built-in Keycloak protocol mapper that emits a flat top-level "roles" claim
(OpenSearch's roles_key extraction has no dotted-path support -- confirmed
against the actual 2.19.1 security-plugin source, see docs/gotchas.md), a
confidential OIDC client for Dashboards' browser login, and a dedicated
local user for human OpenSearch/Dashboards access.

Deliberately a plain local user, not federated via any {org}-idp and not a
member of any org -- ops/observability access to the event log is a
separate concern from tenant membership in this project's model.
"""
from __future__ import annotations

import state
from keycloak_admin import AdminClient, PLATFORM_REALM, admin_token

# Dev-only secrets, same pattern as every other client in this project
# (product-app-secret-dev-only etc.) -- not for production use.
DASHBOARDS_CLIENT_SECRET = "opensearch-dashboards-secret-dev-only"
DASHBOARDS_REDIRECT_URI = "http://localhost:5601/*"


def main() -> None:
    admin = AdminClient(token=admin_token())

    # 1. Realm role. Matches OpenSearch's bundled demo roles_mapping.yml,
    # which already maps backend_role "admin" -> the built-in all_access
    # role -- so this needs zero OpenSearch-side role-mapping edits.
    if state.has("opensearch-admin-role.id"):
        role_id = state.load("opensearch-admin-role.id")
        print(f"admin realm role already provisioned ({role_id}), skipping")
    else:
        admin.post(
            f"/admin/realms/{PLATFORM_REALM}/roles",
            json={"name": "admin", "description": "Grants OpenSearch all_access via roles_mapping.yml's admin backend-role binding"},
        )
        role = admin.get(f"/admin/realms/{PLATFORM_REALM}/roles/admin").json()
        state.save("opensearch-admin-role.id", role["id"])
        print(f"created realm role admin ({role['id']})")

    # 2. Client scope + built-in realm-role mapper, emitting a flat top-level
    # "roles" claim (not e.g. realm_access.roles) on both ID and access
    # tokens -- Dashboards forwards the ID token as bearer for real browser
    # logins, a client_credentials grant presents its own access token
    # directly, so both need the claim.
    if state.has("opensearch-roles-scope.id"):
        scope_id = state.load("opensearch-roles-scope.id")
        print(f"opensearch-roles client scope already provisioned ({scope_id}), skipping")
    else:
        scope_resp = admin.post(
            f"/admin/realms/{PLATFORM_REALM}/client-scopes",
            json={
                "name": "opensearch-roles",
                "protocol": "openid-connect",
                "attributes": {"include.in.token.scope": "true", "display.on.consent.screen": "false"},
            },
        )
        scope_id = AdminClient.location_id(scope_resp)
        state.save("opensearch-roles-scope.id", scope_id)
        print(f"created client scope opensearch-roles ({scope_id})")

        admin.post(
            f"/admin/realms/{PLATFORM_REALM}/client-scopes/{scope_id}/protocol-mappers/models",
            json={
                "name": "opensearch-roles-mapper",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-usermodel-realm-role-mapper",
                "config": {
                    "usermodel.realmRoleMapping.rolePrefix": "",
                    "multivalued": "true",
                    "claim.name": "roles",
                    "jsonType.label": "String",
                    "access.token.claim": "true",
                    "id.token.claim": "true",
                    "userinfo.token.claim": "true",
                },
            },
        )
        print("added opensearch-roles-mapper (built-in oidc-usermodel-realm-role-mapper)")

    # 3. Dashboards OIDC client (confidential, browser login only).
    if state.has("opensearch-dashboards.uuid"):
        print("opensearch-dashboards client already provisioned, skipping")
    else:
        resp = admin.post(
            f"/admin/realms/{PLATFORM_REALM}/clients",
            json={
                "clientId": "opensearch-dashboards",
                "publicClient": False,
                "secret": DASHBOARDS_CLIENT_SECRET,
                "standardFlowEnabled": True,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": False,
                "redirectUris": [DASHBOARDS_REDIRECT_URI],
                "webOrigins": ["http://localhost:5601"],
                # Same "##"-separated multi-value gotcha as product-app's
                # client -- see docs/gotchas.md.
                "attributes": {"post.logout.redirect.uris": "http://localhost:5601##http://localhost:5601/*"},
            },
        )
        client_uuid = AdminClient.location_id(resp)
        state.save("opensearch-dashboards.uuid", client_uuid)
        print(f"created confidential client opensearch-dashboards ({client_uuid})")
        admin.put(f"/admin/realms/{PLATFORM_REALM}/clients/{client_uuid}/default-client-scopes/{scope_id}")
        print("assigned opensearch-roles scope to opensearch-dashboards as default")

    # 4. Dedicated local human user for OpenSearch/Dashboards access.
    if state.has("opensearch.admin.id"):
        print("opensearch.admin user already provisioned, skipping")
        return

    resp = admin.post(
        f"/admin/realms/{PLATFORM_REALM}/users",
        json={
            "username": "opensearch.admin",
            "email": "opensearch.admin@example.com",
            "emailVerified": True,
            "enabled": True,
            "firstName": "OpenSearch",
            "lastName": "Admin",
            "credentials": [{"type": "password", "value": "password123", "temporary": False}],
        },
    )
    user_id = AdminClient.location_id(resp)
    state.save("opensearch.admin.id", user_id)
    print(f"created opensearch.admin ({user_id}), password: password123")

    admin_role = admin.get(f"/admin/realms/{PLATFORM_REALM}/roles/admin").json()
    admin.post(f"/admin/realms/{PLATFORM_REALM}/users/{user_id}/role-mappings/realm", json=[admin_role])
    print("granted realm role admin to opensearch.admin")


if __name__ == "__main__":
    main()
