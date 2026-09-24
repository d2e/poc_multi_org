# Event logging (OpenSearch)

Every event on the `platform` realm (login, logout, org membership changes,
active-org switches, admin actions, etc.) is forwarded to OpenSearch via the
phasetwo `ext-event-webhook` listener (bundled in the `phasetwo-keycloak`
image):

```
Keycloak (platform realm)
  --[ext-event-webhook, POST JSON, HMAC-signed]--> audit-chain-signer (:2023/webhook)
        --[attaches tamper-evident hash chain link, re-signs]--> Vector (http_server source :2021/log/ingest)
              --[elasticsearch sink]--> OpenSearch (:9200, index "keycloak-events")
                                           ^        ^
                              OpenSearch Dashboards |  audit-chain-signer confirms
                              (:5601) for browsing  |  the write (GET by id, as
                                                     |  audit_confirmer) before
                                                     |  committing its own chain
                                                     |  state / ACKing Keycloak
```

Keycloak's webhook no longer posts to Vector directly — `audit-chain-signer`
sits in front of it so every event gets a hash chain link (see
[audit-log-integrity.md](audit-log-integrity.md)) before Vector ever sees
it, then forwards it on, itself HMAC-signed with Vector's own secret so
Vector's existing signature check needs no changes. Vector's `http_server`
source ACKs on receipt, before its `elasticsearch` sink has actually
written anything — so `audit-chain-signer` also confirms the document
landed in OpenSearch directly before treating the forward as successful.
Full detail, including the confirm step's own retry/failure behavior, in
[audit-log-integrity.md](audit-log-integrity.md#pipeline).

`scripts/provision_webhook_opensearch.py` enables the listener on the
`platform` realm and registers the webhook (idempotent — re-running
`scripts/setup.py` won't create duplicates), HMAC-signed (`authType: hmac`)
so audit-chain-signer and org-cache-watcher only accept payloads Keycloak
actually sent — see [opensearch-auth.md](opensearch-auth.md#webhook-authentication-keycloak--vector--org-cache-watcher).
Ingestion is handled by
[Vector](https://vector.dev/) (open source, `timberio/vector` image),
configured declaratively in `opensearch/vector.yaml` — an `http_server`
source with the `json` codec (accepts a single JSON object per request, which
is what the phasetwo webhook sends) piped into an `elasticsearch` sink
pointed at OpenSearch. No custom code.

(OpenSearch's own [Data Prepper](https://opensearch.org/docs/latest/data-prepper/)
was tried first, but its HTTP source only accepts JSON *arrays* per request,
so every webhook POST — a single object — was rejected with a 400. See
[gotchas.md](gotchas.md).)

## Org name enrichment (live, event-driven)

Keycloak's webhook events only ever carry an org *UUID*, never its name, and
not even in a consistent field:
- org-membership admin events embed it in `resourcePath`
  (`orgs/{id}/members/{userId}`)
- the active-organization switch event carries it in
  `details.new_active_organization_id` / `details.previous_active_organization_id`

`opensearch/vector.yaml` resolves whichever is present via a `remap`
transform against a small `orgs` enrichment table
(`opensearch/org-lookup.csv`, id→name→tenant), adding `org_name` /
`new_active_organization_name` / `previous_active_organization_name` fields.

**Tenant enrichment**, added alongside org-name enrichment (see
[multi-env-tenants.md](multi-env-tenants.md) for the full tenant/env
model): the same `orgs` lookup table also carries each org's `tenant`
custom attribute (empty for standalone tenants like `org1`/`acme`, e.g.
`globex` for the `globex-*` env-orgs), and the same `remap` transform
copies it onto a unified top-level `tenant` field whenever any org is
resolved — including **login events**, which carry no org id at all,
resolved instead via `details.identity_provider` (the broker alias, which
in this project's convention is always exactly the org name). One field to
filter/aggregate on regardless of event type, rather than one per event
shape.

That CSV is kept in sync by `scripts/org_cache_watcher.py`, a small
always-on service (container `org-cache-watcher`) that:
1. On startup, does one bootstrap `GET /realms/platform/orgs` to seed the
   cache with orgs that already existed (retries until the realm exists, so
   it doesn't care about startup ordering against the provisioning scripts).
2. Is registered as a *second* webhook target
   (`provision_webhook_opensearch.py` registers both this and Vector — every
   event fans out to both), and reacts to `admin.ORGANIZATION-CREATE/UPDATE/
   DELETE` events as they happen — an observer, not a poller.
3. Rewrites the CSV in place on every change.

Vector runs with `--watch-config`, which hot-reloads both its own config and
enrichment table files on change (stable since Vector 0.46) — so org
create/rename/delete propagates into `org_name` on new events with no
restart on either side. Two non-obvious things had to be worked around to
get this far — see [gotchas.md](gotchas.md) (rename-based atomic writes
silently break Vector's file watch after the first swap; Docker single-file
bind mounts can't be renamed onto).

**Provisioning order matters**: `setup.py` registers the webhook
(`provision_webhook_opensearch.py`) *before* any org is created
(`provision_platform_orgs.py`) — see that file's docstring. If the watcher's
webhook isn't registered yet when an org is created, Keycloak fires (and
loses) that creation event before anything is listening for it, and the
one-shot bootstrap can't retroactively recover it.

This is eventually consistent, not instantaneous: an event fired in the same
instant as the org's own creation (e.g. `provision_demo_user.py`'s
membership PUTs, which run moments after `provision_platform_orgs.py`
creates the org during `setup.py`) can beat Vector's first reload of the
freshly-bootstrapped CSV and resolve to a `null` `org_name`. It self-heals
immediately — the next event referencing that org resolves correctly — this
just isn't something to expect on the very first event after a cold
`setup.py` run.

## Verify it's working

```bash
# trigger an event by logging in as any demo user, then:
curl -s http://localhost:9200/keycloak-events*/_search?pretty
```

Or use the OpenSearch REST API directly for more targeted queries:
```bash
# list the index and its stats
curl -s "http://localhost:9200/_cat/indices/keycloak-events?v"

# total document count
curl -s "http://localhost:9200/keycloak-events/_count"

# most recent 5 events, sorted by time
curl -s "http://localhost:9200/keycloak-events/_search?size=5&sort=timestamp:desc"

# filter by a specific field
curl -s "http://localhost:9200/keycloak-events/_search" -H 'Content-Type: application/json' -d '{
  "query": {"match": {"authDetails.username": "alice@org1.example.com"}}
}'

# aggregate: count events by type (note .keyword -- see "field mapping" below)
curl -s "http://localhost:9200/keycloak-events/_search" -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {"by_type": {"terms": {"field": "type.keyword", "size": 30}}}
}'
```

**Field mapping note**: OpenSearch's dynamic mapping stores string fields
(`type`, `org_name`, `authDetails.username`, `resourcePath`, ...) as `text`
by default (for full-text search), but also auto-adds a `.keyword`
sub-field (exact-match, unanalyzed) alongside each one. Aggregations and
exact-match filters need the `.keyword` variant — a plain `terms` aggregation
on `type` (not `type.keyword`) fails with `illegal_argument_exception:
Text fields are not optimised for operations that require per-document field
data`.

See [dashboards.md](dashboards.md) for the provisioned OpenSearch Dashboards
dashboard, and [testing.md](testing.md) for browser-based verification steps.

## macOS/Colima gotcha

OpenSearch requires `vm.max_map_count >= 262144` on the host. If OpenSearch
fails to start with a `max virtual memory areas` error, run:
```bash
colima ssh -- sudo sysctl -w vm.max_map_count=262144
```
(this doesn't persist across Colima VM restarts — re-run it if needed).

Both `pgdata` (Postgres) and `osdata` (OpenSearch) are named Docker volumes,
so events and users survive `docker compose restart` / `down` + `up -d` —
only `docker compose down -v` wipes them.
