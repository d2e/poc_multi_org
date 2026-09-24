# Multi-Org Phase Two Keycloak POC

Local Phase Two (`keycloak-orgs`) Keycloak with 7 organizations (4
standalone tenants + 1 tenant with 3 environment-orgs), each backed by a
federated IdP realm, the Active Organization feature (including per-org
roles), every realm event streamed into OpenSearch with a live dashboard,
and a demo multi-tenant product app showing both subdomain-based IdP
routing and multi-environment org switching.

## Quick start

```bash
pip install -r requirements.txt
python3 scripts/setup.py    # idempotent; brings up docker-compose + provisions everything
python3 scripts/verify.py   # automated smoke test (login, org switch, subdomain routing, multi-env switching)
```

**Demo index (every URL + credential in this project, one page):** http://localhost:3000/

Admin console: http://localhost:8082 (`admin` / `admin`)
Events dashboard: http://localhost:5601/app/dashboards#/view/keycloak-events-dashboard — log in via Keycloak (`opensearch.admin` / `password123`)
Tenant routing demo: http://acme.sample.127.0.0.1.nip.io:3000
Multi-env tenant demo: http://globex.sample.127.0.0.1.nip.io:3000
Support console (CIBA support-delegation demo): http://localhost:3001 — see docs/ciba-support-delegation.md

Test users (all password `password123`):
| User | How they log in | Ends up in |
|---|---|---|
| `alice@org1.example.com` | federated via org1-idp | org1 |
| `alice@org2.example.com` | federated via org2-idp | org2 |
| `alice@org3.example.com` | federated via org3-idp | org3 |
| `alice@acme.example.com` | federated via acme-idp (or visit the tenant routing demo URL above) | acme |
| `alice@globex.example.com` | federated via globex-idp (or visit the multi-env demo URL above) | globex-dev (admin), can switch to globex-stage (developer) / globex-prod (viewer) |
| `shared.user` | local password login | org1 + org2 (pre-seeded) |

```bash
docker compose down -v   # removes containers + postgres/opensearch volumes (wipes all data)
rm -rf scripts/.state    # scripts track created realm/org/client/webhook IDs here
```

## Documentation

- [**docs/architecture.md**](docs/architecture.md) — realms, login flow,
  Active Organization, container topology, why there are two Keycloak URLs
- [**docs/event-logging.md**](docs/event-logging.md) — the Keycloak →
  Vector → OpenSearch pipeline, live org-name enrichment
- [**docs/dashboards.md**](docs/dashboards.md) — the provisioned OpenSearch
  Dashboards dashboard, panels, troubleshooting
- [**docs/opensearch-auth.md**](docs/opensearch-auth.md) — Keycloak-backed
  OIDC login for OpenSearch/Dashboards, `vector_writer`'s scoped access
- [**docs/audit-log-integrity.md**](docs/audit-log-integrity.md) — tamper-evident
  hash chain on `keycloak-events`, how tampering is detected and what algorithm is used
- [**docs/tenant-routing.md**](docs/tenant-routing.md) — subdomain-based
  IdP routing demo (`acme`, `kc_idp_hint`, the `product-app` container)
- [**docs/multi-env-tenants.md**](docs/multi-env-tenants.md) — one tenant,
  multiple env-orgs (`globex-dev/stage/prod`), org-scoped roles, switching
  between them
- [**docs/ciba-support-delegation.md**](docs/ciba-support-delegation.md) —
  support requesting user-approved delegated access via OIDC CIBA
- [**docs/scripts.md**](docs/scripts.md) — every Python script, what it
  does, provisioning order
- [**docs/testing.md**](docs/testing.md) — `verify.py`, manual browser
  walkthrough, troubleshooting login failures
- [**docs/gotchas.md**](docs/gotchas.md) — every non-obvious thing found
  building this, for future reference

See [`CLAUDE.md`](CLAUDE.md) for the condensed version aimed at an AI
assistant picking up work on this project.
