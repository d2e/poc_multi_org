"""Creates the "globex" multi-environment tenant demo (see
docs/multi-env-tenants.md): one federated IdP realm shared by three
environments (dev/stage/prod), each its own Keycloak org tagged with a
custom `tenant` attribute, each with its own org-scoped role for the demo
user -- proving a user can switch between same-tenant orgs (environments)
and hold a different role in each.

Sequencing problem this solves: alice@globex.example.com is a *federated*
user, so her platform-realm account doesn't exist until her first broker
login (same as org1/2/3/acme's users) -- but pre-granting her
globex-stage/globex-prod membership+roles during provisioning needs her
platform-realm user ID to already exist. Solved by driving one real login
ourselves via browser_login.PlainCookieJarSession (the same machinery
verify.py uses for testing), which bootstraps her account through the
existing post-org-broker-login flow, then granting the rest directly via
admin API.
"""
from __future__ import annotations

import requests

import state
from browser_login import PlainCookieJarSession, form_action, tenant_get
from keycloak_admin import AdminClient, KC_INTERNAL_URL, KC_URL, PLATFORM_REALM, admin_token

TENANT = "globex"
DOMAIN = "globex.example.com"
REALM = f"{TENANT}-idp"
USER = f"alice@{DOMAIN}"
PRODUCT_APP_URL = "http://127.0.0.1:3000"
TENANT_HOST = f"{TENANT}.sample.127.0.0.1.nip.io:3000"

# env org name -> org role granted to alice in that env
ENVS = {
    f"{TENANT}-dev": "admin",
    f"{TENANT}-stage": "developer",
    f"{TENANT}-prod": "viewer",
}
PRIMARY_ENV = f"{TENANT}-dev"  # the only one with a broker/IdP link
ROLE_NAMES = ("admin", "developer", "viewer")


def _create_idp_realm(admin: AdminClient) -> None:
    if admin.exists(f"/admin/realms/{REALM}"):
        print(f"realm {REALM} already exists, skipping")
        return

    admin.post("/admin/realms", json={"realm": REALM, "enabled": True, "sslRequired": "none"})
    print(f"created realm {REALM}")

    redirect = f"{KC_URL}/realms/{PLATFORM_REALM}/broker/{PRIMARY_ENV}/endpoint"
    admin.post(
        f"/admin/realms/{REALM}/clients",
        json={
            "clientId": "platform-broker",
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": False,
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": False,
            "redirectUris": [redirect],
            "secret": f"brokersecret-{TENANT}",
            # See the post.logout.redirect.uris gotcha in docs/gotchas.md --
            # unset, this rejects every post-logout redirect that reaches
            # this client during federated logout.
            "attributes": {"post.logout.redirect.uris": f"{redirect}/*"},
        },
    )
    print(f"created broker client in {REALM}")

    admin.post(
        f"/admin/realms/{REALM}/users",
        json={
            "username": USER,
            "email": USER,
            "emailVerified": True,
            "enabled": True,
            "firstName": "Alice",
            "lastName": "Globex",
            "credentials": [{"type": "password", "value": "password123", "temporary": False}],
        },
    )
    print(f"created test user {USER} in {REALM} (password: password123)")


def _create_envs(admin: AdminClient) -> dict[str, str]:
    """Returns {env_org_name: org_id}."""
    org_ids: dict[str, str] = {}
    for env in ENVS:
        idfile = f"{env}.id"
        if state.has(idfile):
            org_ids[env] = state.load(idfile)
            print(f"org {env} already provisioned (id={org_ids[env]}), skipping")
            continue

        resp = admin.post(
            f"/realms/{PLATFORM_REALM}/orgs",
            json={
                "name": env,
                "displayName": env.replace("-", " ").title(),
                "domains": [f"{env}.{DOMAIN}"],
                "attributes": {"tenant": [TENANT]},
            },
        )
        org_id = AdminClient.location_id(resp)
        state.save(idfile, org_id)
        org_ids[env] = org_id
        print(f"created org {env} -> {org_id} (tenant={TENANT})")

    return org_ids


def _link_primary_env_idp(admin: AdminClient, primary_org_id: str) -> None:
    if admin.exists(f"/realms/{PLATFORM_REALM}/orgs/{primary_org_id}/idps/{PRIMARY_ENV}"):
        print(f"idp {PRIMARY_ENV} already linked, skipping")
        return

    disc = requests.get(f"{KC_URL}/realms/{REALM}/.well-known/openid-configuration", timeout=10).json()
    admin.post(
        f"/realms/{PLATFORM_REALM}/orgs/{primary_org_id}/idps",
        json={
            "alias": PRIMARY_ENV,
            "displayName": f"{TENANT.title()} SSO",
            "providerId": "oidc",
            "enabled": True,
            "trustEmail": True,
            "firstBrokerLoginFlowAlias": "first broker login",
            "postBrokerLoginFlowAlias": "post org broker login",
            "config": {
                "clientId": "platform-broker",
                "clientSecret": f"brokersecret-{TENANT}",
                "authorizationUrl": disc["authorization_endpoint"],
                "tokenUrl": f"{KC_INTERNAL_URL}/realms/{REALM}/protocol/openid-connect/token",
                "logoutUrl": disc["end_session_endpoint"],
                "jwksUrl": f"{KC_INTERNAL_URL}/realms/{REALM}/protocol/openid-connect/certs",
                "userInfoUrl": f"{KC_INTERNAL_URL}/realms/{REALM}/protocol/openid-connect/userinfo",
                "useJwksUrl": "true",
                "validateSignature": "true",
                "syncMode": "IMPORT",
                "defaultScope": "openid email profile",
            },
        },
    )
    print(f"created+linked idp {PRIMARY_ENV} -> org {primary_org_id}")


def _create_org_roles(admin: AdminClient, org_ids: dict[str, str]) -> None:
    for env, org_id in org_ids.items():
        existing = {r["name"] for r in admin.get(f"/realms/{PLATFORM_REALM}/orgs/{org_id}/roles").json()}
        for role in ROLE_NAMES:
            if role in existing:
                continue
            admin.post(
                f"/realms/{PLATFORM_REALM}/orgs/{org_id}/roles",
                json={"name": role, "description": f"{role} access in {env}"},
            )
        print(f"ensured roles {ROLE_NAMES} exist in {env}")


def _bootstrap_alice_via_login() -> None:
    """Drive one real login as alice through the primary env's broker --
    creates her platform-realm account and her PRIMARY_ENV membership via
    the existing post-org-broker-login flow, exactly as a real user's first
    visit would.
    """
    session = PlainCookieJarSession()
    login_resp = tenant_get(session, f"{PRODUCT_APP_URL}/login", TENANT_HOST, PRODUCT_APP_URL, allow_redirects=False)
    kc_auth_url = login_resp.headers.get("Location")
    if not kc_auth_url:
        raise SystemExit(f"could not start login for {USER}: product-app /login returned no redirect")

    hop = session.get(kc_auth_url, allow_redirects=False)
    idp_login_url = session.get(hop.headers.get("Location"), allow_redirects=False).headers.get("Location")
    if not idp_login_url or REALM not in idp_login_url:
        raise SystemExit(f"kc_idp_hint did not route to {REALM}: {idp_login_url}")

    login_page = session.get(idp_login_url)
    resp = session.post(
        form_action(login_page.text),
        data={"username": USER, "password": "password123"},
        allow_redirects=False,
    )
    location = resp.headers.get("Location")
    for _ in range(6):
        if not location or TENANT_HOST in location:
            break
        location = session.get(location, allow_redirects=False).headers.get("Location")
    if not location or TENANT_HOST not in location:
        raise SystemExit(f"bootstrap login for {USER} did not complete (final: {location})")

    tenant_get(session, location, TENANT_HOST, PRODUCT_APP_URL, allow_redirects=False)
    print(f"bootstrapped {USER}'s platform account via one real login through {PRIMARY_ENV}")


def main() -> None:
    admin = AdminClient(token=admin_token())

    _create_idp_realm(admin)
    org_ids = _create_envs(admin)
    _link_primary_env_idp(admin, org_ids[PRIMARY_ENV])
    _create_org_roles(admin, org_ids)

    if state.has(f"{TENANT}.user.id"):
        user_id = state.load(f"{TENANT}.user.id")
        print(f"{USER} already bootstrapped (id={user_id}), skipping login")
    else:
        _bootstrap_alice_via_login()
        users = admin.get(f"/admin/realms/{PLATFORM_REALM}/users?username={USER}").json()
        if not users:
            raise SystemExit(f"{USER} still doesn't exist in platform realm after bootstrap login")
        user_id = users[0]["id"]
        state.save(f"{TENANT}.user.id", user_id)

    for env, role in ENVS.items():
        org_id = org_ids[env]
        if env != PRIMARY_ENV:
            admin.put(f"/realms/{PLATFORM_REALM}/orgs/{org_id}/members/{user_id}")
        admin.put(f"/realms/{PLATFORM_REALM}/orgs/{org_id}/roles/{role}/users/{user_id}")
        print(f"granted {USER} role '{role}' in {env}")


if __name__ == "__main__":
    main()
