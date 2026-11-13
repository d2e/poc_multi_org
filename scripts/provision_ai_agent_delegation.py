"""Provisions the AI Agent Token Exchange Delegation workflow (Keycloak 26.8+).

Configures:
1. Realm-level Fine-Grained Admin Permissions (FGAP V2) enabled on `platform`.
2. `ai-agent` confidential client:
   - Service accounts enabled (client_credentials).
   - Standard token exchange enabled (`standard.token.exchange.enabled`).
3. `delegation-demo-app` client:
   - Interactive login with `consentRequired=True` for granting delegation.
4. FGAP V2 policy & permission on `admin-permissions`:
   - `ai-agent-sa-policy` user policy for the AI agent service account.
   - `ai-agent-delegate-users-permission` scope permission granting `delegate`
     on resource type `Users` to `ai-agent-sa-policy`.

This enables users to grant `delegation:client:ai-agent` scope during login,
minting a `may_act` claim into their token, which `ai-agent` then exchanges
via RFC 8693 token exchange for a delegated token carrying an `act` claim.
See docs/ai-agent-delegation.md.
"""
from __future__ import annotations

import requests

import state
from keycloak_admin import AdminClient, PLATFORM_REALM, admin_token

AI_AGENT_CLIENT_ID = "ai-agent"
AI_AGENT_CLIENT_SECRET = "ai-agent-secret-dev-only"

DEMO_APP_CLIENT_ID = "delegation-demo-app"
DEMO_APP_CLIENT_SECRET = "delegation-demo-app-secret-dev-only"


def main() -> None:
    token = admin_token()
    admin = AdminClient(token=token)

    # 1. Enable FGAP V2 on the realm if not already enabled
    realm = admin.get(f"/admin/realms/{PLATFORM_REALM}").json()
    if not realm.get("adminPermissionsEnabled"):
        realm["adminPermissionsEnabled"] = True
        admin.put(f"/admin/realms/{PLATFORM_REALM}", json=realm)
        print(f"enabled adminPermissionsEnabled (FGAP V2) on {PLATFORM_REALM} realm")
    else:
        print(f"adminPermissionsEnabled already enabled on {PLATFORM_REALM} realm")

    # 2. Provision ai-agent client
    ai_clients = admin.get(f"/admin/realms/{PLATFORM_REALM}/clients?clientId={AI_AGENT_CLIENT_ID}").json()
    if not ai_clients:
        resp = admin.post(
            f"/admin/realms/{PLATFORM_REALM}/clients",
            json={
                "clientId": AI_AGENT_CLIENT_ID,
                "publicClient": False,
                "secret": AI_AGENT_CLIENT_SECRET,
                "serviceAccountsEnabled": True,
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "attributes": {
                    "standard.token.exchange.enabled": "true",
                },
            },
        )
        ai_client_uuid = AdminClient.location_id(resp)
        print(f"created client {AI_AGENT_CLIENT_ID} ({ai_client_uuid}) with token exchange enabled")
    else:
        ai_client_uuid = ai_clients[0]["id"]
        # Ensure standard token exchange attribute is set
        client_data = ai_clients[0]
        attrs = client_data.setdefault("attributes", {})
        if attrs.get("standard.token.exchange.enabled") != "true":
            attrs["standard.token.exchange.enabled"] = "true"
            admin.put(f"/admin/realms/{PLATFORM_REALM}/clients/{ai_client_uuid}", json=client_data)
            print(f"updated client {AI_AGENT_CLIENT_ID} attributes to enable standard token exchange")
        else:
            print(f"client {AI_AGENT_CLIENT_ID} already provisioned")

    state.save("ai-agent.uuid", ai_client_uuid)

    # 3. Provision delegation-demo-app client (interactive login + consent)
    demo_clients = admin.get(f"/admin/realms/{PLATFORM_REALM}/clients?clientId={DEMO_APP_CLIENT_ID}").json()
    if not demo_clients:
        resp = admin.post(
            f"/admin/realms/{PLATFORM_REALM}/clients",
            json={
                "clientId": DEMO_APP_CLIENT_ID,
                "publicClient": False,
                "secret": DEMO_APP_CLIENT_SECRET,
                "standardFlowEnabled": True,
                "directAccessGrantsEnabled": True,
                "consentRequired": True,
                "redirectUris": ["http://localhost:3000/*", "http://localhost:8082/*"],
            },
        )
        demo_client_uuid = AdminClient.location_id(resp)
        print(f"created client {DEMO_APP_CLIENT_ID} ({demo_client_uuid}) with consentRequired=True")
    else:
        demo_client_uuid = demo_clients[0]["id"]
        demo_data = demo_clients[0]
        if not demo_data.get("consentRequired"):
            demo_data["consentRequired"] = True
            admin.put(f"/admin/realms/{PLATFORM_REALM}/clients/{demo_client_uuid}", json=demo_data)
            print(f"updated client {DEMO_APP_CLIENT_ID} with consentRequired=True")
        else:
            print(f"client {DEMO_APP_CLIENT_ID} already provisioned")

    state.save("delegation-demo-app.uuid", demo_client_uuid)

    # 4. FGAP V2 authorization on admin-permissions
    ap_clients = admin.get(f"/admin/realms/{PLATFORM_REALM}/clients?clientId=admin-permissions").json()
    if not ap_clients:
        raise RuntimeError("admin-permissions client not found after enabling adminPermissionsEnabled")
    ap_id = ap_clients[0]["id"]

    # Service account user for ai-agent
    sa_user = admin.get(f"/admin/realms/{PLATFORM_REALM}/clients/{ai_client_uuid}/service-account-user").json()
    sa_user_id = sa_user["id"]

    # Check/create user policy for the service account user
    policies = admin.get(f"/admin/realms/{PLATFORM_REALM}/clients/{ap_id}/authz/resource-server/policy").json()
    sa_policy = next((p for p in policies if p["name"] == "ai-agent-sa-policy"), None)
    if not sa_policy:
        resp = admin.post(
            f"/admin/realms/{PLATFORM_REALM}/clients/{ap_id}/authz/resource-server/policy/user",
            json={
                "name": "ai-agent-sa-policy",
                "type": "user",
                "logic": "POSITIVE",
                "users": [sa_user_id],
            },
        )
        sa_policy = resp.json()
        print(f"created FGAP user policy 'ai-agent-sa-policy' ({sa_policy['id']})")
    else:
        print("FGAP user policy 'ai-agent-sa-policy' already exists")

    # Get delegate scope ID
    scopes = admin.get(f"/admin/realms/{PLATFORM_REALM}/clients/{ap_id}/authz/resource-server/scope").json()
    delegate_scope = next((s for s in scopes if s["name"] == "delegate"), None)
    if not delegate_scope:
        raise RuntimeError("delegate scope not found in admin-permissions authz scopes")

    # Check/create scope permission on Users resource type
    perms = admin.get(f"/admin/realms/{PLATFORM_REALM}/clients/{ap_id}/authz/resource-server/permission/scope").json()
    delegate_perm = next((p for p in perms if p["name"] == "ai-agent-delegate-users-permission"), None)
    if not delegate_perm:
        resp = requests.post(
            f"http://localhost:8082/admin/realms/{PLATFORM_REALM}/clients/{ap_id}/authz/resource-server/permission/scope",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": "ai-agent-delegate-users-permission",
                "type": "scope",
                "resourceType": "Users",
                "logic": "POSITIVE",
                "decisionStrategy": "UNANIMOUS",
                "scopes": [delegate_scope["id"]],
                "policies": [sa_policy["id"]],
            },
            timeout=10,
        )
        resp.raise_for_status()
        delegate_perm = resp.json()
        print(f"created FGAP scope permission 'ai-agent-delegate-users-permission' ({delegate_perm['id']})")
    else:
        print("FGAP scope permission 'ai-agent-delegate-users-permission' already exists")

    print(f"AI Agent Token Exchange Delegation provisioned successfully.")


if __name__ == "__main__":
    main()
