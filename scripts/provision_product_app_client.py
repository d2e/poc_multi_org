"""Creates/updates the confidential "product-app" client used by the demo
tenant-subdomain-routing app (app/product_app.py) -- see
docs/tenant-routing.md and docs/multi-env-tenants.md.

Confidential (not public like poc-app) because this is a real server-side
app that can hold a secret, unlike poc-app's browser/direct-grant demos.
Also assigns the existing "active-organization" client scope (created by
provision_active_org_client.py, which must run before this) so the ID token
carries the active_organization claim the demo app displays after login.

Fetch-mutate-PUT rather than create-once-and-skip: every tenant subdomain
that gets a product-app demo (acme, globex, ...) needs its own *exact*
entries here -- Keycloak has no host-wildcard redirect URIs, only trailing
path wildcards within one exact host -- so this must stay re-runnable as
TENANT_URLS grows, not just on first provisioning.
"""
from __future__ import annotations

import state
from keycloak_admin import AdminClient, PLATFORM_REALM, admin_token

# Fixed dev secret, same pattern as the org broker clients (brokersecret-*) --
# not for production use, just reproducible across `docker compose down -v`.
CLIENT_SECRET = "product-app-secret-dev-only"

TENANT_URLS = [
    "http://acme.sample.127.0.0.1.nip.io:3000",
    "http://globex.sample.127.0.0.1.nip.io:3000",
]


def main() -> None:
    admin = AdminClient(token=admin_token())

    redirect_uris = [f"{url}/callback" for url in TENANT_URLS]
    # Multiple post-logout redirect uris are "##"-separated in this single
    # string attribute (Keycloak has no native list field for it, unlike
    # redirectUris/webOrigins) -- confirmed via Keycloak's own admin
    # console behavior, not assumed.
    post_logout_uris = "##".join(f"{url}/*" for url in TENANT_URLS)

    if state.has("product-app.uuid"):
        client_uuid = state.load("product-app.uuid")
        client = admin.get(f"/admin/realms/{PLATFORM_REALM}/clients/{client_uuid}").json()
        client["redirectUris"] = sorted(set(client.get("redirectUris", [])) | set(redirect_uris))
        client.setdefault("attributes", {})["post.logout.redirect.uris"] = post_logout_uris
        admin.put(f"/admin/realms/{PLATFORM_REALM}/clients/{client_uuid}", json=client)
        print(f"updated product-app client redirect/logout URIs for {len(TENANT_URLS)} tenant(s)")
        return

    resp = admin.post(
        f"/admin/realms/{PLATFORM_REALM}/clients",
        json={
            "clientId": "product-app",
            "publicClient": False,
            "secret": CLIENT_SECRET,
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False,
            "redirectUris": redirect_uris,
            # Without this, Keycloak's RP-initiated logout endpoint rejects
            # post_logout_redirect_uri (defaults to allowing none at all,
            # not falling back to redirectUris) and /logout can't redirect
            # back into the app.
            "attributes": {"post.logout.redirect.uris": post_logout_uris},
        },
    )
    client_uuid = AdminClient.location_id(resp)
    state.save("product-app.uuid", client_uuid)
    print(f"created confidential client product-app ({client_uuid})")

    scope_id = state.load("active-organization-scope.id")
    admin.put(f"/admin/realms/{PLATFORM_REALM}/clients/{client_uuid}/default-client-scopes/{scope_id}")
    print("assigned active-organization scope to product-app as default")


if __name__ == "__main__":
    main()
