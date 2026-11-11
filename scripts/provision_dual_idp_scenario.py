"""Provisions a test scenario: ONE org ("dualidp") linked to TWO separate
federated IdP realms claiming the same email domain -- e.g. a merger, or an
in-progress migration from one corporate IdP to another. Used to empirically
determine which IdP phasetwo's Home IdP Discovery picks when an org has more
than one linked IdP, and how to make that deterministic. See
docs/dual-idp-precedence.md for the full findings.

This is a standalone investigation scenario, not part of the core demo --
run manually, not wired into setup.py's STEPS. It does flip one REALM-WIDE
setting (`multipleIdpsEnabled` org config, see below) that persists after
this script runs; confirmed via scripts/verify.py that this is a no-op for
every other org in this project (org1/2/3/acme/globex all have exactly one
linked IdP, so the extra multi-idp code paths this enables never trigger for
them).

Sets up three layers on purpose, matching docs/dual-idp-precedence.md's
three scenarios:
1. Realm's org config `multipleIdpsEnabled` (PUT /realms/{realm}/orgs/config,
   field `multipleIdps`) -- OFF by default. This is what phasetwo actually
   means by "enable multiple idps for an org": with it off, linking a SECOND
   IdP to an org via POST .../orgs/{id}/idps doesn't error, it silently
   *deactivates* (unlinks) whichever IdP was linked before -- confirmed via
   the org's IdP config's `home.idp.discovery.org` attribute disappearing
   from the first one the moment the second is added. This script always
   turns it on (idempotently) before linking the second IdP specifically to
   avoid that trap.
2. Two IdPs linked to the same org, both matching the org's base domain
   (`dualidp.example.com`) with no per-IdP domain of their own -- this is
   the "implicit precedence" case: Home IdP Discovery's `forwardToFirstMatch`
   authenticator config (already true in this project, see
   provision_browser_flow.py) silently auto-redirects to whichever IdP comes
   back first from Keycloak's own query, with no way to reliably predict or
   configure which -- see docs/dual-idp-precedence.md, don't rely on this in
   a real deployment.
3. Each IdP ALSO given its own narrower domain via the `home.idp.discovery.domains`
   IdP config attribute (`alpha.dualidp.example.com` / `beta.dualidp.example.com`,
   both added to the org's own domain list too, since Keycloak's org-domain
   match happens before the per-IdP one) -- this is the actually-recommended
   fix: a login as alice@alpha.dualidp.example.com or
   alice@beta.dualidp.example.com is then routed deterministically, and
   (confirmed) even bypasses forwardToFirstMatch's ambiguity entirely, since
   narrowing brings the candidate list down to exactly one match either way.
"""
from __future__ import annotations

import requests

import state
from keycloak_admin import AdminClient, KC_INTERNAL_URL, KC_URL, PLATFORM_REALM, admin_token

ORG = "dualidp"
BASE_DOMAIN = "dualidp.example.com"
DISPLAY = "Dual IdP Test Co"

# Each IdP is provisioned with its own narrower domain from the start (see
# module docstring, point 3) -- both org.domains and each IdP's own
# home.idp.discovery.domains need it.
IDPS = [
    {
        "alias": "dualidp-alpha",
        "realm": "dualidp-alpha-idp",
        "display": "Dual IdP Test Co (Alpha SSO)",
        "lastName": "Alpha",
        "narrow_domain": "alpha.dualidp.example.com",
    },
    {
        "alias": "dualidp-beta",
        "realm": "dualidp-beta-idp",
        "display": "Dual IdP Test Co (Beta SSO)",
        "lastName": "Beta",
        "narrow_domain": "beta.dualidp.example.com",
    },
]


def _create_idp_realm(admin: AdminClient, idp: dict) -> None:
    realm = idp["realm"]
    alias = idp["alias"]
    secret = f"brokersecret-{alias}"
    redirect = f"{KC_URL}/realms/{PLATFORM_REALM}/broker/{alias}/endpoint"

    if admin.exists(f"/admin/realms/{realm}"):
        print(f"realm {realm} already exists, skipping")
        return

    admin.post("/admin/realms", json={"realm": realm, "enabled": True, "sslRequired": "none"})
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
            "attributes": {"post.logout.redirect.uris": f"{redirect}/*"},
        },
    )
    user = f"alice@{BASE_DOMAIN}"
    admin.post(
        f"/admin/realms/{realm}/users",
        json={
            "username": user,
            "email": user,
            "emailVerified": True,
            "enabled": True,
            "firstName": "Alice",
            "lastName": idp["lastName"],
            "credentials": [{"type": "password", "value": f"{alias}-password123", "temporary": False}],
        },
    )
    print(f"created realm {realm} + broker client + user {user} (password: {alias}-password123)")


def _enable_multiple_idps(admin: AdminClient) -> None:
    cfg = admin.get(f"/realms/{PLATFORM_REALM}/orgs/config").json()
    if cfg.get("multipleIdpsEnabled"):
        print("realm org config: multipleIdpsEnabled already true, skipping")
        return
    cfg["multipleIdpsEnabled"] = True
    admin.put(f"/realms/{PLATFORM_REALM}/orgs/config", json=cfg)
    print("realm org config: multipleIdpsEnabled -> true")


def _link_idp(admin: AdminClient, org_id: str, idp: dict) -> None:
    realm = idp["realm"]
    alias = idp["alias"]
    idfile = f"{alias}.idp-linked"
    if state.has(idfile):
        print(f"idp {alias} already linked to org {ORG}, skipping")
        return

    disc = requests.get(f"{KC_URL}/realms/{realm}/.well-known/openid-configuration", timeout=10).json()
    admin.post(
        f"/realms/{PLATFORM_REALM}/orgs/{org_id}/idps",
        json={
            "alias": alias,
            "displayName": idp["display"],
            "providerId": "oidc",
            "enabled": True,
            "trustEmail": True,
            "firstBrokerLoginFlowAlias": "first broker login",
            "postBrokerLoginFlowAlias": "post org broker login",
            "config": {
                "clientId": "platform-broker",
                "clientSecret": f"brokersecret-{alias}",
                "authorizationUrl": disc["authorization_endpoint"],
                "tokenUrl": f"{KC_INTERNAL_URL}/realms/{realm}/protocol/openid-connect/token",
                "logoutUrl": disc["end_session_endpoint"],
                "jwksUrl": f"{KC_INTERNAL_URL}/realms/{realm}/protocol/openid-connect/certs",
                "userInfoUrl": f"{KC_INTERNAL_URL}/realms/{realm}/protocol/openid-connect/userinfo",
                "useJwksUrl": "true",
                "validateSignature": "true",
                "syncMode": "IMPORT",
                "defaultScope": "openid email profile",
                # Narrows this specific IdP to its own sub-domain -- see
                # docs/dual-idp-precedence.md's "recommended fix". Requires
                # multipleIdpsEnabled (set above) to actually take effect.
                "home.idp.discovery.domains": idp["narrow_domain"],
            },
        },
    )
    state.save(idfile, alias)
    print(f"linked idp {alias} -> org {ORG} (narrowed to {idp['narrow_domain']})")


def main() -> None:
    admin = AdminClient(token=admin_token())

    for idp in IDPS:
        _create_idp_realm(admin, idp)

    # Must happen before the SECOND idp is linked below, or linking it
    # silently deactivates the first -- see module docstring, point 1.
    _enable_multiple_idps(admin)

    if state.has(f"{ORG}.id"):
        org_id = state.load(f"{ORG}.id")
        print(f"org {ORG} already exists (id={org_id})")
    else:
        domains = [BASE_DOMAIN] + [idp["narrow_domain"] for idp in IDPS]
        resp = admin.post(
            f"/realms/{PLATFORM_REALM}/orgs",
            json={"name": ORG, "displayName": DISPLAY, "domains": domains},
        )
        org_id = AdminClient.location_id(resp)
        state.save(f"{ORG}.id", org_id)
        print(f"created org {ORG} -> {org_id} (domains: {domains})")

    for idp in IDPS:
        _link_idp(admin, org_id, idp)

    print()
    print(f"Org '{ORG}' now has {len(IDPS)} linked IdPs: {[i['alias'] for i in IDPS]}")
    print("Run scripts/test_dual_idp_precedence.py to observe how Home IdP Discovery resolves each case.")


if __name__ == "__main__":
    main()
