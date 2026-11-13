"""Full provisioning entry point. Run: python3 scripts/setup.py"""
from __future__ import annotations

import subprocess
from pathlib import Path

import provision_active_org_client
import provision_ai_agent_delegation
import provision_browser_flow
import provision_ciba_support
import provision_dashboards
import provision_demo_user
import provision_globex_tenant
import provision_idp_realms
import provision_opensearch_auth
import provision_platform_orgs
import provision_platform_realm
import provision_product_app_client
import provision_service_account_client
import provision_webhook_opensearch
import restart_opensearch_dashboards
from keycloak_admin import wait_for_keycloak

REPO_ROOT = Path(__file__).parent.parent

STEPS = [
    provision_idp_realms,
    provision_platform_realm,
    # opensearch-dashboards' container started (via `docker compose up -d`,
    # below) before the "platform" realm existed -- its OIDC plugin does a
    # one-time synchronous discovery-doc fetch at boot and crashes fatally
    # on the inevitable 404. Restarting it now, with the realm it needs
    # already in place, is what gives it a real chance to come up clean.
    # See docs/opensearch-auth.md.
    restart_opensearch_dashboards,
    # Webhook registration must happen before any org is created -- see the
    # docstring in provision_platform_orgs.py.
    provision_webhook_opensearch,
    provision_platform_orgs,
    provision_browser_flow,
    provision_active_org_client,
    # Independent of everything above except the realm itself -- Keycloak
    # identity for OpenSearch/Dashboards login (see docs/opensearch-auth.md).
    # Ordered before provision_dashboards (below), which now authenticates
    # against Dashboards using the OpenSearch bootstrap admin credential.
    provision_opensearch_auth,
    # Needs the active-organization client scope (just above) and the acme
    # org (provision_platform_orgs, above) to already exist.
    provision_product_app_client,
    # Needs product-app (just above) to already exist -- it drives one real
    # login through it to bootstrap alice@globex.example.com's account. See
    # provision_globex_tenant.py's docstring.
    provision_globex_tenant,
    provision_demo_user,
    provision_dashboards,
    # Independent of everything above -- just needs the platform realm (and
    # its auto-generated platform-realm client in master) to exist.
    provision_service_account_client,
    # Also independent -- just needs the platform realm. See
    # docs/ciba-support-delegation.md.
    provision_ciba_support,
    # AI Agent Token Exchange Delegation (Keycloak 26.8+ preview). See
    # docs/ai-agent-delegation.md.
    provision_ai_agent_delegation,
]


def main() -> None:
    # --build: `docker compose up -d` alone never rebuilds an image for a
    # container that already exists, even if app/Dockerfile's build context
    # (app/product_app.py) changed since the image was last built -- a
    # `setup.py` re-run after editing product-app's code would otherwise
    # silently keep serving the stale build. See docs/gotchas.md.
    subprocess.run(["docker", "compose", "up", "-d", "--build"], cwd=REPO_ROOT, check=True)
    wait_for_keycloak()

    for step in STEPS:
        step.main()

    print()
    print("=" * 58)
    print("Done. Demo index (every URL + credential, one page): http://localhost:3000/")
    print("Keycloak admin console: http://localhost:8082 (admin/admin)")
    print("Platform realm:  http://localhost:8082/admin/master/console/#/platform")
    print("Try a login:     http://localhost:8082/realms/platform/account (client-less account console)")
    print("  - alice@org1.example.com / password123  -> federated via org1-idp")
    print("  - alice@org2.example.com / password123  -> federated via org2-idp")
    print("  - alice@org3.example.com / password123  -> federated via org3-idp")
    print("  - shared.user / password123             -> local user, member of org1 + org2")
    print("See README.md for the active-organization switch demo.")
    print("Tenant subdomain routing demo: http://acme.sample.127.0.0.1.nip.io:3000")
    print("  - alice@acme.example.com / password123  -> federated via acme-idp, no email prompt")
    print("See docs/tenant-routing.md.")
    print("Multi-env tenant demo: http://globex.sample.127.0.0.1.nip.io:3000")
    print("  - alice@globex.example.com / password123 -> globex-dev (admin), can switch to")
    print("    globex-stage (developer) / globex-prod (viewer)")
    print("See docs/multi-env-tenants.md.")
    print("OpenSearch:            https://localhost:9200 (event index: keycloak-events)")
    print("  - admin / <OPENSEARCH_INITIAL_ADMIN_PASSWORD from docker-compose.yml> -> break-glass only")
    print("OpenSearch Dashboards: http://localhost:5601 -- log in via Keycloak:")
    print("  - opensearch.admin / password123")
    print("Events dashboard:      http://localhost:5601/app/dashboards#/view/keycloak-events-dashboard")
    print("See README.md for the event-logging verification steps and docs/opensearch-auth.md for the auth setup.")
    print("Service-to-service Admin API access: client_credentials grant against")
    print("  http://localhost:8082/realms/master/protocol/openid-connect/token")
    print("  client_id=automation-svc / client_secret=automation-svc-secret-dev-only")
    print("  -> scoped to the platform realm's Admin API. See docs/scripts.md.")
    print("CIBA support-delegated access demo (CLI):")
    print("  python3 scripts/support_console.py alice@acme.example.com")
    print("  -> approve/deny in product-app once logged in.")
    print("CIBA support-delegated access demo (browser UI): http://localhost:3001")
    print("  -> same flow, browser-driven. See docs/ciba-support-delegation.md.")
    print("AI Agent Token Exchange Delegation demo (RFC 8693 'act' claim):")
    print("  python3 scripts/demo_ai_agent_delegation.py")
    print("  -> see docs/ai-agent-delegation.md.")
    print("=" * 58)


if __name__ == "__main__":
    main()
