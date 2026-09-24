"""Creates the "platform" realm (just the realm -- orgs are provisioned
separately by provision_platform_orgs.py, deliberately later in setup.py's
step order; see that file for why).
"""
from __future__ import annotations

from keycloak_admin import AdminClient, PLATFORM_REALM, admin_token


def main() -> None:
    admin = AdminClient(token=admin_token())

    if admin.exists(f"/admin/realms/{PLATFORM_REALM}"):
        print(f"realm {PLATFORM_REALM} already exists, skipping realm creation")
        return

    admin.post(
        "/admin/realms",
        json={"realm": PLATFORM_REALM, "enabled": True, "sslRequired": "none", "registrationAllowed": False},
    )
    print(f"created realm {PLATFORM_REALM}")


if __name__ == "__main__":
    main()
