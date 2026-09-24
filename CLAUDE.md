# poc_multi_org — Project Instructions

Local Phase Two (`keycloak-orgs`) Keycloak POC: 7 organizations (4
standalone tenants + 1 tenant with 3 environment-orgs) each backed by a
federated IdP realm, the Active Organization feature (including per-org
roles), every realm event streamed live into OpenSearch with a provisioned
dashboard, and a demo multi-tenant product app showing both subdomain-based
IdP routing and multi-environment org switching.

Full docs live under [`docs/`](docs/) — read the relevant one before making a
change in that area, and update it in the same commit as any
architecture/command change (matches the global convention: docs rot fast
otherwise). Index:

| Doc | Covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Realms, orgs, login flow, Active Organization, container topology, `KC_URL` vs `KC_INTERNAL_URL` |
| [docs/event-logging.md](docs/event-logging.md) | Keycloak → Vector → OpenSearch pipeline, live org-name enrichment via `org_cache_watcher.py` |
| [docs/dashboards.md](docs/dashboards.md) | The provisioned OpenSearch Dashboards dashboard, panels, troubleshooting |
| [docs/opensearch-auth.md](docs/opensearch-auth.md) | Keycloak-backed OIDC login for OpenSearch/Dashboards, `vector_writer`'s scoped access, security-plugin gotchas |
| [docs/audit-log-integrity.md](docs/audit-log-integrity.md) | Tamper-evident hash chain on `keycloak-events`, `audit_chain_signer.py`, `verify_audit_chain.py` |
| [docs/tenant-routing.md](docs/tenant-routing.md) | Subdomain-based IdP routing demo (`acme`, `kc_idp_hint`, the `product-app` container) |
| [docs/multi-env-tenants.md](docs/multi-env-tenants.md) | One tenant, multiple env-orgs (`globex-dev/stage/prod`), org-scoped roles, switching between them |
| [docs/ciba-support-delegation.md](docs/ciba-support-delegation.md) | Support requesting user-approved delegated access via OIDC CIBA (`support_console.py`, `ciba_notifier.py`) |
| [docs/scripts.md](docs/scripts.md) | Every Python script, purpose, provisioning order |
| [docs/testing.md](docs/testing.md) | `verify.py`, manual browser walkthrough, troubleshooting |
| [docs/gotchas.md](docs/gotchas.md) | Every non-obvious thing found building this — **check here first** before assuming something is a new bug |

## Tech stack / conventions

- **Python only, not shell.** This project is meant to move toward
  production eventually; scripts are written as reusable, importable
  modules (`scripts/keycloak_admin.py`'s `AdminClient`), not one-off `.sh`
  glue. This was an explicit ask — don't regress to shell scripts for new
  automation here.
- All provisioning is idempotent — `python3 scripts/setup.py` is safe to
  re-run on an already-provisioned stack. If you add a new provisioning
  step, it must check before creating (see existing `provision_*.py` for the
  pattern: state file, or query Keycloak/OSD directly).
- Docker Compose for everything (`docker-compose.yml`); no host-installed
  Keycloak/OpenSearch.
- `docs/gotchas.md` exists because several of these bugs were re-discovered
  the hard way more than once during development (broken healthcheck,
  broken cookie jar, broken dashboard schema). **Check it before debugging
  something that looks like a new, surprising failure** — there's a decent
  chance it's already documented there with the fix.

## Commands

```bash
pip install -r requirements.txt
python3 scripts/setup.py              # bring up + provision everything (idempotent)
python3 scripts/verify.py             # smoke test: federated login + org switch
python3 scripts/provision_dashboards.py  # re-provision just the OpenSearch dashboard
docker compose down -v                # full teardown, wipes all data
```

Demo index (every URL + credential, one page): `http://localhost:3000/`
Admin console: `http://localhost:8082` (`admin`/`admin`)
Events dashboard: `http://localhost:5601/app/dashboards#/view/keycloak-events-dashboard` — log in via Keycloak (`opensearch.admin`/`password123`); see [docs/opensearch-auth.md](docs/opensearch-auth.md)
Tenant routing demo: `http://acme.sample.127.0.0.1.nip.io:3000` (`alice@acme.example.com`/`password123`)
Multi-env tenant demo: `http://globex.sample.127.0.0.1.nip.io:3000` (`alice@globex.example.com`/`password123`)
Support console (CIBA support-delegation demo): `http://localhost:3001` — see [docs/ciba-support-delegation.md](docs/ciba-support-delegation.md)

## Verifying UI/browser-facing changes

Don't declare a browser-facing change (anything touching
`provision_dashboards.py`, Keycloak login flows, etc.) working from the API
response alone — this bit us twice on the dashboard (two schema bugs that
produced *zero visible error on the page*, only in the browser console).
Use a real headless browser to confirm rendering:
```bash
python3 -m venv /tmp/pw-venv && /tmp/pw-venv/bin/pip install playwright
/tmp/pw-venv/bin/python -m playwright install chromium
# then drive it with sync_playwright(), check page.on("console")/page.on("pageerror")
```
See `docs/dashboards.md` for the specific bugs this caught.
