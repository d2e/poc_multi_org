# AGENTS.md — Agent Guidelines & Engineering Standards

> Open format guidelines, operating instructions, and constraints for autonomous AI coding agents working on `poc_multi_org`.

## Overview

Local Phase Two (`keycloak-orgs`) Keycloak POC: 7 organizations (4 standalone tenants + 1 tenant with 3 environment-orgs) each backed by a federated IdP realm, the Active Organization feature (including per-org roles), every realm event streamed live into OpenSearch with a provisioned dashboard, a demo multi-tenant product app showing both subdomain-based IdP routing and multi-environment org switching, and AI Agent Token Exchange Delegation (RFC 8693 `act` claim).

Full docs live under [`docs/`](docs/) — agents **must** read the relevant one before making a change in that area, and update it in the same commit as any architecture/command change (matches the project convention: docs rot fast otherwise).

---

## Documentation & Architecture Map

| Document | Covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Realms, orgs, login flow, Active Organization, container topology, `KC_URL` vs `KC_INTERNAL_URL` |
| [docs/event-logging.md](docs/event-logging.md) | Keycloak → Vector → OpenSearch pipeline, live org-name enrichment via `org_cache_watcher.py` |
| [docs/dashboards.md](docs/dashboards.md) | The provisioned OpenSearch Dashboards dashboard, panels, troubleshooting |
| [docs/opensearch-auth.md](docs/opensearch-auth.md) | Keycloak-backed OIDC login for OpenSearch/Dashboards, `vector_writer`'s scoped access, security-plugin gotchas |
| [docs/audit-log-integrity.md](docs/audit-log-integrity.md) | Tamper-evident hash chain on `keycloak-events`, `audit_chain_signer.py`, `verify_audit_chain.py` |
| [docs/tenant-routing.md](docs/tenant-routing.md) | Subdomain-based IdP routing demo (`acme`, `kc_idp_hint`, the `product-app` container) |
| [docs/multi-env-tenants.md](docs/multi-env-tenants.md) | One tenant, multiple env-orgs (`globex-dev/stage/prod`), org-scoped roles, switching between them |
| [docs/dual-idp-precedence.md](docs/dual-idp-precedence.md) | Test scenario: one org with TWO linked federated IdPs — precedence rules, the `multipleIdpsEnabled` org config, and per-IdP domain narrowing |
| [docs/ciba-support-delegation.md](docs/ciba-support-delegation.md) | Support requesting user-approved delegated access via OIDC CIBA (`support_console.py`, `ciba_notifier.py`) |
| [docs/ai-agent-delegation.md](docs/ai-agent-delegation.md) | AI Agent Token Exchange Delegation (Keycloak 26.8+ RFC 8693 `act` claim, `may_act`, FGAP V2, sub-agent multi-hop delegation) |
| [docs/scripts.md](docs/scripts.md) | Every Python script, purpose, provisioning order |
| [docs/testing.md](docs/testing.md) | `verify.py`, manual browser walkthrough, troubleshooting |
| [docs/gotchas.md](docs/gotchas.md) | Every non-obvious thing found building this — **check here first** before assuming something is a new bug |

---

## Core Engineering Rules & Constraints

1. **Python Only, No Shell Scripts**:
   - This project is designed to move toward production. All scripts are written as reusable, importable modules (`scripts/keycloak_admin.py`'s `AdminClient`), not one-off `.sh` glue.
   - Do not regress to shell scripts for new automation.
2. **Strict Idempotency**:
   - `python3 scripts/setup.py` must be safe to re-run on an already-provisioned stack.
   - Any new provisioning step must check before creating (see existing `provision_*.py` pattern: state file under `scripts/.state/`, or querying Keycloak/OpenSearch directly).
3. **Containerized Topology**:
   - All services run via Docker Compose (`docker-compose.yml`); no host-installed Keycloak, OpenSearch, or Postgres dependencies.
4. **Mandatory Gotchas Consultation**:
   - `docs/gotchas.md` contains hard-won lessons (broken healthcheck, broken cookie jars, broken OpenSearch dashboard schema, CIBA delivery modes, host vs internal URLs).
   - **Check `docs/gotchas.md` before debugging** any unexpected failure — it is likely documented with the exact fix.

---

## Dev Environment & CLI Commands

```bash
# Setup dependencies
pip install -r requirements.txt

# Provision stack idempotently
python3 scripts/setup.py                 # bring up + provision everything

# Run automated verification suite
python3 scripts/verify.py                # smoke tests: federated login, org switch, subdomain routing, multi-env, AI delegation

# Specific component provisioning / teardown
python3 scripts/provision_dashboards.py   # re-provision just the OpenSearch dashboard
python3 scripts/demo_ai_agent_delegation.py # run standalone AI agent delegation flow
docker compose down -v                   # full teardown, wipes all state and volumes
```

---

## Verification & Testing Protocols

### 1. Automated Verification
Always execute the automated verification test suite before declaring a task complete:
```bash
python3 scripts/verify.py
```

### 2. Browser / UI-Facing Verification
Do **not** declare a browser-facing change (anything touching `provision_dashboards.py`, Keycloak login flows, templates in `app/product_app.py`, etc.) working from API responses alone. In this repository, silent JavaScript/CSS failures or OpenSearch dashboard schema mismatches produce zero HTTP errors while breaking user rendering.

Use a real headless browser session to confirm rendering:
```bash
python3 -m venv /tmp/pw-venv && /tmp/pw-venv/bin/pip install playwright
/tmp/pw-venv/bin/python -m playwright install chromium
# drive with sync_playwright(), attach handlers to page.on("console") and page.on("pageerror")
```
Refer to `docs/dashboards.md` for specific browser-side schema traps caught this way.

---

## Demo Endpoints & Credentials Matrix

| Service / Demo | URL | Credentials / Notes |
|---|---|---|
| **Demo Index** | `http://localhost:3000/` | Single landing page with all demo links & users |
| **Admin Console** | `http://localhost:8082` | `admin` / `admin` |
| **Events Dashboard** | `http://localhost:5601/app/dashboards#/view/keycloak-events-dashboard` | Log in via Keycloak: `opensearch.admin` / `password123` (see [docs/opensearch-auth.md](docs/opensearch-auth.md)) |
| **Tenant Routing Demo** | `http://acme.sample.127.0.0.1.nip.io:3000` | `alice@acme.example.com` / `password123` |
| **Multi-Env Tenant Demo** | `http://globex.sample.127.0.0.1.nip.io:3000` | `alice@globex.example.com` / `password123` (admin in dev, developer in stage, viewer in prod) |
| **Support Console (CIBA)** | `http://localhost:3001` | No login needed; enter user email to request delegated access (see [docs/ciba-support-delegation.md](docs/ciba-support-delegation.md)) |
| **AI Agent Delegation (RFC 8693)** | `http://localhost:3000/ai-agent` or `python3 scripts/demo_ai_agent_delegation.py` | Interactive consent & token exchange flow; see [docs/ai-agent-delegation.md](docs/ai-agent-delegation.md) |
