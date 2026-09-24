"""Wires phasetwo's "Home IdP Discovery" (ext-auth-home-idp-discovery) authenticator
into a copy of the browser flow, replacing the combined Username+Password form
with [Home IdP Discovery] -> [Password Form], and binds it as platform's browser flow.

Ordering gotcha (see README): after adding an execution, always raise/lower its
priority via the raise-priority/lower-priority endpoints -- and when setting
.requirement, PUT the FULL execution representation (not just {id,requirement}),
otherwise Keycloak silently re-sorts the flow and breaks execution order.
"""
from __future__ import annotations

from keycloak_admin import AdminClient, PLATFORM_REALM, admin_token

FLOW_ALIAS = "org discovery browser"
FORMS_ALIAS = "org discovery browser forms"


def _find(execs: list[dict], **match) -> dict | None:
    for e in execs:
        if all(e.get(k) == v for k, v in match.items()):
            return e
    return None


def main() -> None:
    admin = AdminClient(token=admin_token())

    # Keycloak has no "get flow by alias" endpoint (a GET on
    # .../flows/{alias} 404s regardless of whether it exists) -- list flows
    # and check by alias instead.
    flows = admin.get(f"/admin/realms/{PLATFORM_REALM}/authentication/flows").json()
    if _find(flows, alias=FLOW_ALIAS):
        print(f"flow '{FLOW_ALIAS}' already exists, skipping flow surgery")
        return

    admin.post(
        f"/admin/realms/{PLATFORM_REALM}/authentication/flows/browser/copy",
        json={"newName": FLOW_ALIAS},
    )
    print(f"copied 'browser' -> '{FLOW_ALIAS}'")

    execs = admin.get(f"/admin/realms/{PLATFORM_REALM}/authentication/flows/{FLOW_ALIAS}/executions").json()
    upw_form = _find(execs, providerId="auth-username-password-form")
    native_org_subflow = _find(execs, displayName=f"{FLOW_ALIAS} Organization")

    admin.delete(f"/admin/realms/{PLATFORM_REALM}/authentication/executions/{upw_form['id']}")
    print("removed combined Username Password Form")

    admin.post(
        f"/admin/realms/{PLATFORM_REALM}/authentication/flows/{FORMS_ALIAS}/executions/execution",
        json={"provider": "auth-password-form"},
    )
    admin.post(
        f"/admin/realms/{PLATFORM_REALM}/authentication/flows/{FORMS_ALIAS}/executions/execution",
        json={"provider": "ext-auth-home-idp-discovery"},
    )
    print("added Password Form + Home IdP Discovery executions")

    execs2 = admin.get(f"/admin/realms/{PLATFORM_REALM}/authentication/flows/{FORMS_ALIAS}/executions").json()
    pw_id = _find(execs2, providerId="auth-password-form")["id"]
    hidp_id = _find(execs2, providerId="ext-auth-home-idp-discovery")["id"]

    admin.post(
        f"/admin/realms/{PLATFORM_REALM}/authentication/executions/{hidp_id}/config",
        json={
            "alias": "home-idp-discovery-config",
            "config": {
                "userAttribute": "email",
                "forwardToLinkedIdp": "false",
                "requireVerifiedEmail": "false",
                "requireVerifiedDomain": "false",
                "bypassLoginPage": "false",
                "forwardToFirstMatch": "true",
            },
        },
    )
    print("configured Home IdP Discovery (email-domain based, unverified domains OK)")

    # Move Home IdP Discovery to the very top of the forms subflow (above the 2FA
    # conditional subflow), then Password Form right after it.
    admin.post(f"/admin/realms/{PLATFORM_REALM}/authentication/executions/{hidp_id}/raise-priority")
    admin.post(f"/admin/realms/{PLATFORM_REALM}/authentication/executions/{hidp_id}/raise-priority")
    admin.post(f"/admin/realms/{PLATFORM_REALM}/authentication/executions/{pw_id}/raise-priority")
    print("reordered: Home IdP Discovery, Password Form, [2FA subflow]")

    execs3 = admin.get(f"/admin/realms/{PLATFORM_REALM}/authentication/flows/{FORMS_ALIAS}/executions").json()
    for exec_id in (hidp_id, pw_id):
        row = _find(execs3, id=exec_id)
        row["requirement"] = "REQUIRED"
        admin.put(f"/admin/realms/{PLATFORM_REALM}/authentication/flows/{FORMS_ALIAS}/executions", json=row)
    print("set Home IdP Discovery + Password Form to REQUIRED")

    # Disable Keycloak's native (built-in, unrelated) "Organization Identity-First
    # Login" branch -- we use phasetwo's keycloak-orgs model, not native orgs.
    if native_org_subflow:
        native_org_subflow["requirement"] = "DISABLED"
        admin.put(
            f"/admin/realms/{PLATFORM_REALM}/authentication/flows/{FLOW_ALIAS}/executions",
            json=native_org_subflow,
        )
        print("disabled native Organization Identity-First Login branch")

    admin.put(f"/admin/realms/{PLATFORM_REALM}", json={"browserFlow": FLOW_ALIAS})
    print(f"bound '{FLOW_ALIAS}' as the realm's browser flow")


if __name__ == "__main__":
    main()
