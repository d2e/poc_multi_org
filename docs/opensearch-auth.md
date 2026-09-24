# OpenSearch / OpenSearch Dashboards authentication

Both OpenSearch and OpenSearch Dashboards previously ran with security
fully disabled (`DISABLE_SECURITY_PLUGIN`/`DISABLE_SECURITY_DASHBOARDS_PLUGIN`)
— anyone reaching `:9200` or `:5601` had unrestricted read/write/delete
access to the `keycloak-events` index (every login, logout, org switch,
admin action across every tenant) and the Dashboards admin UI, with zero
authentication. This document covers how that was replaced with **Keycloak
itself** as the identity provider, consistent with the rest of this
project.

## The model

| Actor | Auth method | Why |
|---|---|---|
| A human logging into Dashboards | Keycloak OIDC (browser SSO) | Real identity, real login flow, same IdP as everything else in this project |
| Vector (event ingest) | Static internal OpenSearch user (`vector_writer`) | Vector's `elasticsearch` sink has no OAuth2/bearer-refresh support at all — a Keycloak access token would 401 within minutes. See "Vector's write path" below. |
| `scripts/provision_dashboards.py` | HTTP Basic as the OpenSearch bootstrap `admin` user | It only ever calls Dashboards' own admin-level saved-objects API — no user-identity-aware calls, no OIDC flow needed for a script |
| Bootstrap `admin` internal user | Break-glass only | Not used by any application component day-to-day |
| Keycloak → Vector / org-cache-watcher (webhooks) | HMAC-SHA256 shared secret (`authType: hmac`) | See "Webhook authentication" below |

One OpenSearch `openid`-type auth domain handles **both** the human
browser-login path and would handle a machine `client_credentials` bearer
token if one were ever added — confirmed from the actual 2.19.1 security
plugin source that `openid`-type authenticators support dynamic JWKS key
fetch/rotation (unlike the plain `jwt` type, which only takes a static
key). No separate `jwt_auth_domain` is needed.

## Setup

- **`opensearch/security-config/`**: the OpenSearch image's bundled demo
  security config, modified to add:
  - `config.yml`'s `openid_auth_domain`, pointed at
    `http://keycloak:8080/realms/platform/.well-known/openid-configuration`
    (container-internal — this is fetched server-side by the OpenSearch
    JVM, never by a browser).
  - `internal_users.yml`/`roles.yml`/`roles_mapping.yml`'s `vector_writer`
    user, scoped to a `keycloak_events_writer` role that can only
    `create_index`/`write` on `keycloak-events*` — not `all_access`.
  - Bind-mounted over `/usr/share/opensearch/config/opensearch-security/`
    in `docker-compose.yml`; auto-applied by the entrypoint on a fresh
    security index (i.e. after `docker compose down -v`, since the index
    lives in the `osdata` volume) — no `securityadmin.sh` needed for this
    project's normal test cycle.
- **`opensearch/opensearch_dashboards.yml`**: a full config replacement
  (not env vars — OIDC needs more keys than the image's env-var allowlist
  covers), setting `opensearch_security.auth.type: ["basicauth", "openid"]`
  + `multiple_auth_enabled: true` (both required together — Dashboards
  throws at startup with just an array and no flag), the OIDC client
  id/secret/connect_url/base_redirect_url, and
  `opensearch_security.multitenancy.enabled: false` explicitly (the
  image's shipped default is `true`, with a Private/Global tenant-picker
  screen after login this project has no use for).
- **`scripts/provision_opensearch_auth.py`**: Keycloak-side provisioning —
  a plain realm role `admin` (matches `roles_mapping.yml`'s existing
  `backend_roles: ["admin"] -> all_access` binding with zero OpenSearch-side
  edits), a client scope + Keycloak's *built-in* `oidc-usermodel-realm-role-mapper`
  emitting a flat top-level `roles` claim, a confidential
  `opensearch-dashboards` OIDC client, and a dedicated local user
  `opensearch.admin` / `password123` — deliberately not federated, not a
  member of any org, since ops/observability access to the event log is a
  separate concern from tenant membership in this project's model.

## Manual verification

```bash
python3 scripts/setup.py
```

Open `http://localhost:5601` — click **"Log in with single sign-on"**
(shown alongside the local basic-auth form, since both auth types are
active), log in as `opensearch.admin` / `password123` via Keycloak, land
back in Dashboards with the provisioned events dashboard intact, no
tenant-selection screen.

```bash
curl -k https://localhost:9200/_cluster/health          # 401, no credentials
curl    http://localhost:9200/_cluster/health            # fails outright, TLS now required
curl -k -u admin:<OPENSEARCH_INITIAL_ADMIN_PASSWORD> https://localhost:9200/_cluster/health   # 200
```

```bash
# forged, unsigned webhook payloads must now be rejected:
curl -X POST http://localhost:2022/webhook -d '{"resourceType":"ORGANIZATION"}'  # 401
curl -X POST http://localhost:2021/log/ingest -d '{"type":"fake.forged.event"}'  # 200 (Vector's
  # http_server source always ACKs), but confirm it's silently dropped, not indexed:
curl -k -u admin:<pw> "https://localhost:9200/keycloak-events/_search" \
  -d '{"query":{"match":{"type":"fake.forged.event"}}}'   # hits.total.value: 0
```

## Webhook authentication (Keycloak → Vector / org-cache-watcher)

Keycloak's `ext-event-webhook` listener (phasetwo's `keycloak-events`
extension) previously POSTed every realm event to Vector (`:2021`) and
org-cache-watcher (`:2022`) with `authType: none` — zero verification.
Anything on the docker network could forge audit-log entries or poison the
org-name/tenant lookup table Vector's enrichment depends on.

The extension supports three `authType`s: `none`, `hmac` (a shared secret,
signature sent as `X-Keycloak-Signature`), and `bearer` (a short-lived JWT
signed by the realm's own key, verified against its JWKS — no shared
secret needed at all). `bearer` fits this project's "use Keycloak
identity" theme better, but **neither receiver can actually verify it**:
Vector's generic `http_server` source/VRL has no RSA/JWKS verification
capability (confirmed — no `jwt`/`rsa`/`verify` functions in VRL), and
giving `org_cache_watcher.py` that capability would mean pulling in
`PyJWT`/`cryptography`, giving up its deliberate "stdlib only, no build
step" design. `hmac` is used instead — a scoped exception, same reasoning
already applied to Vector's own `vector_writer` OpenSearch credential.

**The signature format wasn't documented precisely enough to trust
blindly** (the extension's README says "the signature according to
RFC2104," without stating hex vs. base64) — confirmed empirically instead:
registered a real webhook with a test secret, triggered a real Keycloak
event, and compared the actual `X-Keycloak-Signature` header byte-for-byte
against both encodings computed independently. It's **lowercase hex** of
`HMAC-SHA256(secret, raw_request_body)`.

**Verifying against the exact raw bytes matters for Vector specifically.**
Its source was originally configured with `decoding: codec: json`, which
parses the body immediately — re-serializing an already-parsed JSON object
for signature comparison is not guaranteed to reproduce Keycloak's exact
bytes (key order, spacing, escaping can all differ), which would make
every signature check fail closed. Fixed by switching to `codec: bytes`
(keeping the raw body in `.message`) and doing the HMAC check *before*
explicitly parsing it in a new `verify_signature` VRL transform, ahead of
the existing `enrich_org_name` transform.

**Vector's `${VAR}` environment variable interpolation is disabled by
default and fails silently, not loudly.** Confirmed empirically: without
`--dangerously-allow-env-var-interpolation` on the command line, a config
value like `"${WEBHOOK_HMAC_SECRET_VECTOR}"` is left as that literal
string — no error, no warning — which would make the signature check
compare against the wrong value and silently drop every real event as
"invalid." The flag name is alarming; the actual exposure here is limited
to this one dev-only shared secret, not arbitrary environment secrets.

`org_cache_watcher.py` verifies the same way using only stdlib
`hmac`/`hashlib` (no new dependency, preserving its existing design), and
returns a clean `401` on a missing or mismatched signature rather than
silently dropping — there's no VRL-style "drop and move on" here, this is
a webhook Keycloak retries with backoff on non-2xx, so a loud `401` is the
useful signal.

## Vector's write path: a deliberate exception, not an oversight

Vector's `elasticsearch` sink supports only `basic` or `aws` auth
strategies — no `bearer` strategy, no OAuth2/client-credentials token
refresh of any kind. A static Keycloak access token would 401 within
Keycloak's default token lifetime (a few minutes — see the
access-token-expiry gotcha this project already hit once in
`app/product_app.py`), silently breaking event ingestion until noticed.

Rather than build a token-refresher sidecar (a new always-on process whose
own failure mode is "ingestion quietly stops," trading one real problem for
another), Vector authenticates via `vector_writer`, a static internal
OpenSearch user scoped only to write `keycloak-events*`. This is the one
place this design doesn't route through Keycloak identity — a conscious,
user-confirmed choice given the alternative, not an inconsistency. Worth
re-visiting only if Vector's sink ever gains native OAuth2 support
upstream.

## Gotchas found building this (see also docs/gotchas.md)

- **OpenSearch's static action group names are lowercase**
  (`create_index`, `write`) — confirmed by extracting
  `static_config/static_action_groups.yml` from the actual installed
  plugin jar. Writing `CREATE_INDEX`/`WRITE` (uppercase, the naming style
  used elsewhere in Keycloak/OpenSearch docs) silently matches nothing —
  Vector's writes failed with `no permissions for [indices:admin/create]`
  even though the role file "looked" correct, with no error pointing at
  the actual typo.
- **`roles_key` extraction has no nested/dotted-path support in 2.19.1** —
  confirmed by reading `AbstractHTTPJwtAuthenticator`'s source directly: it
  does a single flat `claims.getClaim(rolesKey)` lookup. A claim like
  `realm_access.roles` would just look up a literal (nonexistent) key
  named `realm_access.roles` and silently produce zero roles. Fixed with a
  dedicated client-scope mapper emitting a flat top-level `roles` claim
  (Keycloak's built-in `oidc-usermodel-realm-role-mapper` — no custom SPI
  needed).
- **Dashboards' `opensearch_security.openid.base_redirect_url` must be set
  explicitly.** Left unset, Dashboards derives the redirect_uri it sends to
  Keycloak from its own container bind address (`0.0.0.0:5601` per
  `server.host`), producing a redirect Keycloak can never actually reach.
- **`auth.type` as an array requires `multiple_auth_enabled: true`** or
  Dashboards throws `Multiple Authentication Mode is disabled...` at
  startup and never comes up at all — not a graceful fallback to one auth
  method.
- **Dashboards' OIDC plugin does a one-time synchronous fetch of the IdP's
  discovery document at container-boot time, with no retry, and crashes
  fatally if it fails.** This collided directly with this project's own
  provisioning order: `docker compose up -d` starts every container
  (including Dashboards) before `setup.py`'s Python scripts have created
  the `platform` realm, so the discovery-doc fetch always 404s on a
  genuinely fresh stack. Fixed two ways: `scripts/restart_opensearch_dashboards.py`
  restarts the container once the realm actually exists (early in
  `setup.py`'s `STEPS`, well before `provision_dashboards.py` — the step
  that actually needs a working Dashboards — runs), plus
  `restart: on-failure:10` in `docker-compose.yml` as a safety net for a
  standalone `docker compose up -d` run without `setup.py`.
- **Keycloak's own Docker healthcheck had a second bug beyond the
  already-documented `/dev/tcp`-under-`/bin/sh` one**: even after invoking
  `bash -c` correctly, it was hitting `/health/ready` on port **8080**
  (the main HTTP port) — Keycloak 26's health endpoints live on a separate
  **management** interface, port **9000** by default, and 404 on 8080
  regardless of actual health. This had never mattered before (nothing in
  this project gated on Keycloak's healthcheck), but now does:
  Dashboards' fatal-crash-on-unready-Keycloak behavior above means a real,
  working `service_healthy` condition on Keycloak is a hard requirement,
  not a nice-to-have.
