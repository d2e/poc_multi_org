"""Empirically determines which IdP phasetwo's Home IdP Discovery picks when
an org has two linked IdPs (see provision_dual_idp_scenario.py and
docs/dual-idp-precedence.md for the full writeup).

Drives a real login attempt against the `platform` realm and inspects the
redirect Home IdP Discovery issues -- the same browser-simulation approach
scripts/verify.py uses for the single-IdP case, just stopping right after
the first redirect since that's the moment precedence is decided (before any
password is even entered).

Three scenarios, matching provision_dual_idp_scenario.py's setup:
A) A broad-domain login (alice@dualidp.example.com, matches both IdPs
   equally) with forwardToFirstMatch=true (this project's actual, unchanged
   config) -- auto-redirects to ONE of the two, silently, with no picker.
   Confirmed non-obvious: WHICH one depends on the two IdPs' relative link
   order and isn't something this project's config makes deterministic or
   configurable -- treat this as implementation-defined, don't rely on it.
B) The same broad-domain login with forwardToFirstMatch=false -- falls back
   to a real picker page listing both IdPs, letting the user choose.
C) Logins against each IdP's own narrower domain
   (alice@alpha.dualidp.example.com / alice@beta.dualidp.example.com) --
   deterministic exact routing to the matching IdP, confirmed to hold
   regardless of forwardToFirstMatch (narrowing brings the candidate list
   down to exactly one, so the authenticator's `list.size() == 1` shortcut
   auto-redirects either way). This is the actually-recommended setup for
   running two real IdPs under one org unambiguously.

Read-only with respect to running state: restores forwardToFirstMatch to
its original value before exiting either way.
"""
from __future__ import annotations

from browser_login import PlainCookieJarSession, form_action
from keycloak_admin import AdminClient, KC_URL, PLATFORM_REALM, admin_token

FORMS_FLOW = "org discovery browser forms"
IDP_ALIASES = ("dualidp-alpha", "dualidp-beta")


def _find(execs: list[dict], **match) -> dict | None:
    for e in execs:
        if all(e.get(k) == v for k, v in match.items()):
            return e
    return None


def _home_idp_discovery_config_id(admin: AdminClient) -> str:
    execs = admin.get(f"/admin/realms/{PLATFORM_REALM}/authentication/flows/{FORMS_FLOW}/executions").json()
    return _find(execs, providerId="ext-auth-home-idp-discovery")["authenticationConfig"]


def _get_config(admin: AdminClient, config_id: str) -> dict:
    return admin.get(f"/admin/realms/{PLATFORM_REALM}/authentication/config/{config_id}").json()


def _set_forward_to_first_match(admin: AdminClient, config_id: str, value: bool) -> None:
    cfg = _get_config(admin, config_id)
    cfg["config"]["forwardToFirstMatch"] = "true" if value else "false"
    admin.put(f"/admin/realms/{PLATFORM_REALM}/authentication/config/{config_id}", json=cfg)


def attempt_discovery(username: str) -> tuple[str | None, str]:
    """Submits `username` at platform's login page and reports what Home IdP
    Discovery did. Returns (redirected_idp_alias_or_None, raw_page_or_location).
    """
    session = PlainCookieJarSession()
    auth_page = session.get(
        f"{KC_URL}/realms/{PLATFORM_REALM}/protocol/openid-connect/auth",
        params={"client_id": "poc-app", "response_type": "code", "redirect_uri": f"{KC_URL}/", "scope": "openid"},
    )
    resp = session.post(form_action(auth_page.text), data={"username": username}, allow_redirects=False)
    location = resp.headers.get("Location")
    if location:
        for alias in IDP_ALIASES:
            if f"/broker/{alias}/" in location or f"/realms/{alias}-idp/" in location:
                return alias, location
        return None, location
    return None, resp.text


def main() -> None:
    admin = AdminClient(token=admin_token())
    config_id = _home_idp_discovery_config_id(admin)
    original = _get_config(admin, config_id)["config"]["forwardToFirstMatch"]

    print(f"realm org config: {admin.get(f'/realms/{PLATFORM_REALM}/orgs/config').json()}")
    print(f"home-idp-discovery forwardToFirstMatch (original): {original}")
    print()

    try:
        print("=== Scenario A: broad-domain login, forwardToFirstMatch=true (this project's config) ===")
        _set_forward_to_first_match(admin, config_id, True)
        alias, detail = attempt_discovery("alice@dualidp.example.com")
        if alias:
            print(f"RESULT: auto-redirected to '{alias}' with no picker shown -- implementation-defined, don't rely on it.")
        else:
            print(f"RESULT: did not auto-redirect to either dualidp IdP. detail={detail[:300]}")
        print()

        print("=== Scenario B: broad-domain login, forwardToFirstMatch=false ===")
        _set_forward_to_first_match(admin, config_id, False)
        alias, detail = attempt_discovery("alice@dualidp.example.com")
        if alias:
            print(f"RESULT: still auto-redirected to '{alias}' -- unexpected, investigate.")
        else:
            print(f"RESULT: no single redirect -- this is the IdP-picker page (mentions alpha={IDP_ALIASES[0] in detail}, beta={IDP_ALIASES[1] in detail})")
        print()

        print("=== Scenario C: per-IdP narrow-domain logins (the recommended setup) ===")
        _set_forward_to_first_match(admin, config_id, False)  # prove it's deterministic even with this off
        for username, expected in (
            ("alice@alpha.dualidp.example.com", "dualidp-alpha"),
            ("alice@beta.dualidp.example.com", "dualidp-beta"),
        ):
            alias, detail = attempt_discovery(username)
            status = "OK" if alias == expected else "MISMATCH"
            print(f"{status}: {username} -> {alias} (expected {expected})")
    finally:
        _set_forward_to_first_match(admin, config_id, original == "true")
        print()
        print(f"restored forwardToFirstMatch={original}")


if __name__ == "__main__":
    main()
