"""Creates the platform realm's organizations (see keycloak_admin.TENANTS) and
links each to its own federated IdP realm via the keycloak-orgs org-scoped
IdP API.

Split out from realm creation (see provision_platform_realm.py) and run
*after* provision_webhook_opensearch.py in setup.py's step order on purpose:
org_cache_watcher.py's webhook must already be registered before any org is
created, or Keycloak fires (and loses) the admin.ORGANIZATION-CREATE events
before anything is listening for them -- the watcher's one-shot bootstrap
GET only reflects whatever exists at the moment it happens to run, so it
can't retroactively recover events it wasn't registered for.
"""
from __future__ import annotations

import requests

import state
from keycloak_admin import AdminClient, KC_INTERNAL_URL, KC_URL, PLATFORM_REALM, TENANTS, admin_token


def main() -> None:
    admin = AdminClient(token=admin_token())

    for tenant in TENANTS:
        org = tenant["org"]
        idfile = f"{org}.id"

        if state.has(idfile):
            print(f"org {org} already provisioned (id={state.load(idfile)}), skipping")
            continue

        resp = admin.post(
            f"/realms/{PLATFORM_REALM}/orgs",
            json={"name": org, "displayName": tenant["display"], "domains": [tenant["domain"]]},
        )
        org_id = AdminClient.location_id(resp)
        state.save(idfile, org_id)
        print(f"created org {org} -> {org_id}")

        realm = f"{org}-idp"
        disc = requests.get(f"{KC_URL}/realms/{realm}/.well-known/openid-configuration", timeout=10).json()
        auth_url = disc["authorization_endpoint"]
        logout_url = disc["end_session_endpoint"]
        # Backend (server-to-server) endpoints must use the container-internal URL:
        # Keycloak itself can't reach its own host-mapped port from inside the container.
        token_url = f"{KC_INTERNAL_URL}/realms/{realm}/protocol/openid-connect/token"
        jwks_url = f"{KC_INTERNAL_URL}/realms/{realm}/protocol/openid-connect/certs"
        userinfo_url = f"{KC_INTERNAL_URL}/realms/{realm}/protocol/openid-connect/userinfo"

        admin.post(
            f"/realms/{PLATFORM_REALM}/orgs/{org_id}/idps",
            json={
                "alias": org,
                "displayName": f"{tenant['display']} SSO",
                "providerId": "oidc",
                "enabled": True,
                "trustEmail": True,
                "firstBrokerLoginFlowAlias": "first broker login",
                "postBrokerLoginFlowAlias": "post org broker login",
                "config": {
                    "clientId": "platform-broker",
                    "clientSecret": f"brokersecret-{org}",
                    "authorizationUrl": auth_url,
                    "tokenUrl": token_url,
                    "logoutUrl": logout_url,
                    "jwksUrl": jwks_url,
                    "userInfoUrl": userinfo_url,
                    "useJwksUrl": "true",
                    "validateSignature": "true",
                    "syncMode": "IMPORT",
                    "defaultScope": "openid email profile",
                },
            },
        )
        print(f"created+linked idp {org} -> org {org_id}")


if __name__ == "__main__":
    main()
