# Scripts

Everything here is Python (`requirements.txt`: `requests`), not shell — this
project is meant to eventually move toward production, and the scripts are
written to be reusable at that point: a shared `keycloak_admin.AdminClient`,
one script per concern, each independently runnable *and* importable, not
just runnable.

```bash
pip install -r requirements.txt
python3 scripts/setup.py    # idempotent; brings up docker-compose + provisions everything
python3 scripts/verify.py   # automated smoke test (federated login + org switch)
```

## Shared modules

| File | Purpose |
|---|---|
| `scripts/keycloak_admin.py` | `AdminClient` (session + token + get/post/put/delete/exists), `wait_for_keycloak()`, `KC_URL`/`KC_INTERNAL_URL`/`PLATFORM_REALM`/`TENANTS` constants |
| `scripts/state.py` | Tiny key-file store under `scripts/.state/` for idempotency (tracks created realm/org/client/webhook IDs) |
| `scripts/browser_login.py` | `PlainCookieJarSession`, `form_action()`, `tenant_get()` — drives Keycloak's real login HTML/redirect flow from Python. Shared by `verify.py` (testing) and `provision_globex_tenant.py` (bootstrapping a federated user's account — see [multi-env-tenants.md](multi-env-tenants.md)) |

## Provisioning steps (run in this order by `setup.py`)

| Order | Script | Does |
|---|---|---|
| 1 | `provision_idp_realms.py` | Creates one `{org}-idp` realm per `keycloak_admin.TENANTS` entry (org1/org2/org3/acme), each with a broker client + one test user |
| 2 | `provision_platform_realm.py` | Creates the `platform` realm (just the realm — split out from org creation on purpose, see next) |
| 3 | `restart_opensearch_dashboards.py` | Restarts the `opensearch-dashboards` container now that the `platform` realm exists — its OIDC plugin crashes fatally at boot if it doesn't yet. See [opensearch-auth.md](opensearch-auth.md) |
| 4 | `provision_webhook_opensearch.py` | Enables `ext-event-webhook` + registers both webhook targets (Vector, org-cache-watcher). **Must run before any org is created** — see its docstring and [event-logging.md](event-logging.md) |
| 5 | `provision_platform_orgs.py` | Creates org1/org2/org3/acme and links each to its IdP realm |
| 6 | `provision_browser_flow.py` | Copies the `browser` flow, wires in Home IdP Discovery + Password Form, disables Keycloak's native org branch |
| 7 | `provision_active_org_client.py` | Creates the `poc-app` public client + `active-organization` client scope/mapper |
| 8 | `provision_opensearch_auth.py` | Creates the Keycloak-side identity for OpenSearch/Dashboards login: `admin` realm role, `opensearch-roles` client scope + realm-role mapper, the `opensearch-dashboards` OIDC client, and the `opensearch.admin` local user. See [opensearch-auth.md](opensearch-auth.md) |
| 9 | `provision_product_app_client.py` | Creates/updates the confidential `product-app` client's redirect + post-logout URIs for every tenant subdomain in `TENANT_URLS` (currently `acme`, `globex`) — see [tenant-routing.md](tenant-routing.md) |
| 10 | `provision_globex_tenant.py` | Creates the `globex-idp` realm and 3 `globex-*` env-orgs, roles, and alice's memberships — needs `product-app` (step 9) to drive its bootstrap login. See [multi-env-tenants.md](multi-env-tenants.md) |
| 11 | `provision_demo_user.py` | Creates `shared.user`, adds to org1 + org2 |
| 12 | `provision_dashboards.py` | Provisions the OpenSearch Dashboards index pattern + dashboard (see [dashboards.md](dashboards.md)); now authenticates as the OpenSearch bootstrap admin (see [opensearch-auth.md](opensearch-auth.md)) |
| 13 | `provision_service_account_client.py` | Creates the confidential `automation-svc` client (master realm, service accounts enabled) for headless service-to-service Admin API calls, scoped to the `platform` realm via master's auto-generated `platform-realm` client roles — no browser login or master admin username+password needed. Get a token: `client_credentials` grant against `http://localhost:8082/realms/master/protocol/openid-connect/token` with `client_id=automation-svc` / `client_secret=automation-svc-secret-dev-only` (or `keycloak_admin.service_account_token()`) |
| 14 | `provision_ciba_support.py` | Sets `platform`'s CIBA policy (already Keycloak's default on this image, set explicitly anyway) and creates the confidential `support-tool` client (CIBA grant + poll delivery mode) — see [ciba-support-delegation.md](ciba-support-delegation.md) |
| 15 | `provision_ai_agent_delegation.py` | Enables FGAP V2 (`adminPermissionsEnabled`), provisions confidential client `ai-agent` with standard token exchange enabled, provisions `delegation-demo-app` with consent required, and configures FGAP policy `ai-agent-sa-policy` and permission `ai-agent-delegate-users-permission` granting `delegate` scope on `Users`. See [ai-agent-delegation.md](ai-agent-delegation.md) |

Every step is idempotent — checks `scripts/.state/*` or queries Keycloak
directly before creating anything, so re-running `setup.py` on an already-
provisioned stack is a no-op (confirmed: running it twice back-to-back
produces "already exists / already provisioned, skipping" for every step).

## Service-to-service Admin API access (`automation-svc`)

Every other script in this project authenticates as the `admin`/`admin`
master user via `keycloak_admin.admin_token()` (a resource-owner-password
grant against `admin-cli`) — fine for one-off local provisioning, but not a
pattern to reach for from a long-lived script or another service that just
needs to call the `platform` realm's Admin API without holding the actual
master admin credentials or driving a browser login.

`provision_service_account_client.py` creates a confidential client,
`automation-svc`, in the **master** realm with a service account
(`client_credentials` grant — no username/password, no browser, no user
session). Its service account is granted every client role from master's
auto-generated `platform-realm` client (the same 22 roles — `manage-users`,
`manage-realm`, `manage-organizations`, `query-clients`, etc. — the admin
console's own "platform realm admin" assignment uses), so it can fully
administer the `platform` realm's Admin REST API and the `keycloak-orgs`
extension's `/realms/platform/orgs` endpoints, but nothing outside that
realm — it is *not* a master-superadmin credential.

```bash
# get a token
curl -s http://localhost:8082/realms/master/protocol/openid-connect/token \
  -d client_id=automation-svc \
  -d client_secret=automation-svc-secret-dev-only \
  -d grant_type=client_credentials

# use it
curl -s http://localhost:8082/realms/platform/orgs \
  -H "Authorization: Bearer $TOKEN"
```

Or from Python: `keycloak_admin.service_account_token()` returns the bearer
token directly, for use with the existing `AdminClient`.

Fixed dev secret (`automation-svc-secret-dev-only`), same pattern as
`product-app`/the org broker clients — not for production use, just
reproducible across `docker compose down -v`.

**Does not help with OpenSearch Dashboards.** Dashboards' OIDC plugin only
supports the browser authorization-code flow (a real session cookie) — a
`client_credentials` bearer token has nowhere to attach for its UI routes.
For scripted access to OpenSearch/Dashboards data, use the `opensearch.admin`
basic-auth credential directly against the OpenSearch REST API or the
Dashboards saved-objects API instead (see
[opensearch-auth.md](opensearch-auth.md)) — verifying actual on-screen
rendering still needs a real browser session, per the root [AGENTS.md](../AGENTS.md)
UI-verification rule.

## Always-on services (not provisioning scripts, run as containers)

| File | Container | Purpose |
|---|---|---|
| `scripts/org_cache_watcher.py` | `org-cache-watcher` | Keeps Vector's org id→name enrichment table live — see [event-logging.md](event-logging.md) |
| `scripts/audit_chain_signer.py` | `audit-chain-signer` | Sits between Keycloak's webhook and Vector; attaches a tamper-evident hash chain link to every event before forwarding — see [audit-log-integrity.md](audit-log-integrity.md) |
| `scripts/ciba_notifier.py` | `ciba-notifier` | Keycloak's CIBA "Decoupled Auth Server" for this demo — receives Keycloak's pending-approval notification, surfaces it to `product-app`, relays the user's approve/deny decision back — see [ciba-support-delegation.md](ciba-support-delegation.md) |
| `app/product_app.py` | `product-app` | Demo multi-tenant product app: subdomain-based IdP routing ([tenant-routing.md](tenant-routing.md)) and multi-env org switching ([multi-env-tenants.md](multi-env-tenants.md)) |
| `support_console_app/support_console_app.py` | `support-console` | Browser UI for the "support agent" side of the CIBA demo (`http://localhost:3001`) — same flow as `scripts/support_console.py`, browser-driven instead of CLI-driven. Stateless: `auth_req_id` round-trips through the page's own URL/JS, no server-side session. See [ciba-support-delegation.md](ciba-support-delegation.md). |

## AI Agent & Support tooling

| File | Purpose |
|---|---|
| `scripts/demo_ai_agent_delegation.py` | Complete end-to-end demonstration of Keycloak 26.8+ AI Agent Token Exchange Delegation (RFC 8693): user consent for `delegation:client:ai-agent`, `may_act` issuance, client credentials actor token, token exchange, verification of `act` claim, and downstream user-scoped API invocation. See [ai-agent-delegation.md](ai-agent-delegation.md). |
| `scripts/support_console.py` | The "support agent" side of the CIBA demo, as a CLI — run as `python3 scripts/support_console.py <email-or-username>`, requests delegated access, polls for approval, then reads the user's org info once approved. Same flow as `support-console` above, just terminal-driven. See [ciba-support-delegation.md](ciba-support-delegation.md). |

## Testing

| File | Purpose |
|---|---|
| `scripts/verify.py` | Automated smoke test — federated login (`alice@org3.example.com` through Home IdP Discovery → org3-idp → broker callback), active-organization switch, subdomain-based IdP routing (`acme` via `kc_idp_hint`), and multi-env tenant switching (`globex`: login, 2 switches, cross-tenant rejection). See [testing.md](testing.md) for what it does and the bugs found writing it. |
| `scripts/verify_audit_chain.py` | Standalone check (not part of `setup.py`) — walks `keycloak-events` in `audit_seq` order and detects tampering: content edited, documents deleted, or chain links broken. See [audit-log-integrity.md](audit-log-integrity.md). |

## Teardown

```bash
docker compose down -v   # removes containers + postgres/opensearch volumes (wipes all data)
rm -rf scripts/.state    # scripts track created realm/org/client/webhook IDs here
```
