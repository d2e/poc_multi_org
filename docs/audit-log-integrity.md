# Audit log integrity: detecting tampering, not just preventing writes

`keycloak-events` is an audit log — every login, logout, org switch, and
admin action. Restricting *who* can write to it (see
[opensearch-auth.md](opensearch-auth.md)) doesn't answer a different
question: if someone with write access (or a compromised `vector_writer`
credential, or a genuine superuser like the break-glass `admin` user) edits
or deletes a past entry, would anyone notice? Before this, no.

## Pipeline

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      Keycloak (p2-keycloak, realm: platform)            │
│   login / logout / org-switch / admin actions                          │
└──────────────────────────────┬────────────────────────────────────────┘
                                │ ext-event-webhook (HMAC-signed, authType: hmac)
                                │ fan-out to TWO webhook targets:
                ┌───────────────┴───────────────┐
                ▼                               ▼
┌───────────────────────────────────┐   ┌─────────────────────────────────┐
│  p2-audit-chain-signer :2023        │   │   p2-org-cache-watcher :2022    │
│  (single-writer, stateful)          │   │  reacts to admin.ORGANIZATION-  │
│                                       │   │  CREATE/UPDATE/DELETE only      │
│  1. verify HMAC                      │   │  rewrites opensearch/           │
│  2. dedupe by event id (last 500)   │   │    org-lookup.csv in place       │
│  3. atomic: read prev_hash/seq       │   └──────────────┬───────────────────┘
│     → compute audit_seq/hash/       │                  │ hot-reloaded via
│       prev_hash/raw_body             │                  │ --watch-config
│     → forward (re-signed) ──────────┼──┐               ▼
│                                       │  │     (feeds Vector's enrichment
│  4. confirm before commit:           │  │      table remap transform)
│     GET keycloak-events/_doc/<id>    │  │
│     as audit_confirmer               │  │
│     (read-only, get-by-id only —     │  │
│      opensearch/security-config/     │  │
│      roles.yml: keycloak_events_     │  │
│      reader)                         │  │
│     retry up to 10x/0.3s;            │  │
│     not found after retries,         │  │
│     or non-404 error → 502,          │  │
│     state NOT persisted              │  │
│        │                              │  │
│        ▼ confirmed                    │  │
│  5. persist state → 204 Keycloak     │  │
└───────────────────────────────────┘  │
                                          │ POST (re-signed w/ Vector's HMAC secret)
                                          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    p2-vector :2021  (opensearch/vector.yaml)             │
│  http_server source (json codec)                                        │
│    → verify_signature transform (Vector's HMAC secret)                  │
│    ⚠ ACKs on receipt here — BEFORE the sink below actually writes        │
│      anything downstream. This is exactly why step 4 above exists —      │
│      a 2xx from here is not proof of durability.                        │
│    → remap transform: enrich_org_name                                   │
│         .org_id/.org_name/.tenant                    (top-level)        │
│         .details.new_active_organization_name        (nested — excluded │
│         .details.previous_active_organization_name     via              │
│                                                        NESTED_ENRICHMENT │
│                                                        _FIELDS in the    │
│                                                        verifier)         │
│    → elasticsearch sink (action: create, id_key: "id" → dedup on _id)   │
└──────────────────────────────┬────────────────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              p2-opensearch :9200   index "keycloak-events"              │
│   vector_writer:    index/bulk/mapping-put only — no delete/update      │
│   audit_confirmer:  get-by-id only — read, nothing else                  │
└───────────┬───────────────────────────────────────────┬────────────────┘
            │                                            │
            ▼                                            ▼
┌───────────────────────────┐              ┌───────────────────────────────┐
│ p2-opensearch-dashboards   │              │  scripts/verify_audit_chain.py │
│  :5601 (browse/visualize)  │              │  paginated (search_after on    │
│  OIDC login via Keycloak   │              │  audit_seq), not size:10000     │
└───────────────────────────┘              │  checks per audit_seq:         │
                                             │   1. sequence contiguity       │
                                             │   2. chain linkage             │
                                             │   3. raw_body hash match       │
                                             │   4. field consistency         │
                                             │      (nested enrichment        │
                                             │       correctly excluded)      │
                                             └───────────────────────────────┘
```

Known gaps, still open (see "What this doesn't cover" below):
- **No external checkpoint anchor ("Level 2")** — `verify_audit_chain.py`'s
  pass/fail still lives only where the same admin credential that could
  tamper also lives.
- **Confirm-credential misconfiguration has a different failure signature
  than a genuine downstream write failure.** Found during testing: if only
  `audit_confirmer`'s credential is broken (not `vector_writer`'s), Vector
  still writes the document for real while the signer's own confirm check
  502s not knowing that — a document exists but is untracked by the
  signer's state file until manually reconciled. A genuine write failure,
  by contrast, leaves no document at all and confirms cleanly as absent on
  retry. Not yet fixed; the signer doesn't currently distinguish "confirmed
  absent" from "the confirm mechanism itself is broken."

## Level 0: make tampering harder in the first place

Two changes to `vector_writer`'s own access, both in
`opensearch/security-config/roles.yml` and `opensearch/vector.yaml`:

- **`vector_writer`'s role no longer grants delete/update at all.** It
  previously used OpenSearch's built-in `write` action group, which expands
  to `indices:data/write*` — that wildcard also covers
  `indices:data/write/delete` and `indices:data/write/update`, a real gap
  nobody had noticed. The role now lists explicit actions instead:
  `indices:data/write/index*`, `indices:data/write/bulk*` (wildcarded — the
  bulk API's actual permission check is against the shard-level sub-action
  `indices:data/write/bulk[s]`, not just the bare name; granting only the
  bare name 403'd every real write while still correctly blocking
  delete/update, which made the mistake easy to miss), and
  `indices:admin/mapping/put`.
- **Vector's sink uses `action: create` with `id_key: "id"`**, not
  `action: index`. `create` rejects a write to an `_id` that already
  exists (`409 version_conflict_engine_exception`) instead of silently
  overwriting it. `id_key: "id"` maps Keycloak's own event `id` field
  (confirmed present and stable on every event type, unlike the separate
  `uid` field — the extension's own docs describe `uid` as a per-webhook
  *delivery attempt* id, meaning it changes on a retry of the same
  underlying event) to the OpenSearch document `_id`, so `create` has a
  stable value to reject a duplicate against. This also means a legitimate
  webhook retry is correctly deduplicated, not just blocked tampering.

Verified directly: `vector_writer` gets a clean `403` on `_delete_by_query`,
single-document `DELETE`, and `_update`; a genuine duplicate write gets
`409`; normal ingestion still works (`docker logs p2-vector` clean, event
count matches expectations).

**This alone isn't enough** — the break-glass `admin` superuser (or anyone
who obtains its credential) can still edit or delete anything. That's
exactly what Level 1 is for.

## Level 1: tamper-evident hash chain

The standard construction for this (used by Certificate Transparency logs
and AWS CloudTrail's log-file integrity validation):

```
event[n].hash = SHA-256( event[n-1].hash || event[n].raw_content )
```

Each event's hash commits to its own content *and* the previous event's
hash. Altering or deleting any past event breaks the chain for every event
after it — a periodic check that recomputes and compares hashes in order
catches it immediately and names the exact broken link.

### Where it lives: a new service, not a Vector transform

`scripts/audit_chain_signer.py` sits between Keycloak's webhook and Vector.
Keycloak now posts every event here (`:2023`) instead of straight to
Vector; this attaches the chain link, then forwards to Vector's actual
ingest endpoint (re-signing the forwarded request with Vector's own HMAC
secret — see [opensearch-auth.md](opensearch-auth.md) — so Vector's
existing `verify_signature` transform needs no changes at all).

VRL transforms are stateless per event — there's no built-in way for one to
remember "what was the previous event's hash" across invocations or
restarts. That needs a persistent, single-writer service, the same shape as
`org_cache_watcher.py` (state file on a mounted volume, survives container
restarts, resets to genesis on a full `docker compose down -v` along with
everything else).

Each forwarded event gains four fields:

| Field | Meaning |
|---|---|
| `audit_seq` | Monotonically increasing sequence number |
| `audit_prev_hash` | The previous event's `audit_hash` (genesis: 64 zeros) |
| `audit_hash` | `SHA-256(audit_prev_hash + audit_raw_body)` |
| `audit_raw_body` | The *exact* raw bytes Keycloak sent, verbatim |

### Enrichment fields can be nested, not just top-level

`ENRICHMENT_FIELDS` (top-level `org_id`/`org_name`/`tenant`) isn't the only
shape Vector's `enrich_org_name` transform adds fields in --
`opensearch/vector.yaml` also writes `.details.new_active_organization_name`
/ `.details.previous_active_organization_name` *nested inside* the
`details` field, for every active-org-switch event. `verify_audit_chain.py`
tracks these separately in `NESTED_ENRICHMENT_FIELDS` (keyed by the
top-level field name to the set of its enrichment-added subkeys) and strips
them before comparing `details` against `audit_raw_body`'s original copy.
Confirmed live, not theoretical: every one of this project's org-switch
events (21 of the 164 present at the time) was flagging as "tampered" on
`details` before this fix -- a false positive that would bury a real
tampering signal in noise the very first time this was actually needed.

### Why `audit_raw_body` is stored at all, and why the hash alone isn't the real check

The first version of this only re-verified `audit_hash` against
`audit_raw_body` — and completely missed a real test tamper of
`authDetails.username` on a live document, because `audit_raw_body` itself
was never touched. `audit_raw_body` is inert: nobody queries or displays
it, so an attacker tampering with what people actually look at (the
top-level, searchable fields) wouldn't need to touch it at all.

The check that actually matters, and what `scripts/verify_audit_chain.py`
does: **cross-check every top-level field in the live document against
what's embedded in `audit_raw_body`** (excluding the fields Vector's
`enrich_org_name` transform legitimately adds afterward — `org_id`,
`org_name`, `tenant` — and excluding `id`, which Vector's `id_key`
promotes to the document's actual OpenSearch `_id` and strips from
`_source` entirely, confirmed empirically, checked against `_id` instead).
Any mismatch — `authDetails`, `details`, `type`, anything — is tampering,
whether or not `audit_hash` itself still checks out.

### A successful forward to Vector doesn't mean the event survived

Vector's `http_server` source ACKs on receipt of a request, *before* its
`elasticsearch` sink has actually written anything downstream (confirmed --
see "Vector's write path" in [opensearch-auth.md](opensearch-auth.md); its
sink also runs with no disk-backed buffer configured). So a 2xx response
from `audit_chain_signer.py`'s forward to Vector only proves Vector
received the request -- not that the document actually landed in
`keycloak-events`. Left unchecked, a sink-side failure (OpenSearch
backpressure, a transient rejected write) would still let the signer commit
the advanced chain state and `204` Keycloak, permanently losing that event
with nothing but a sequence gap for someone to eventually notice next time
they happen to run `verify_audit_chain.py`.

`audit_chain_signer.py` closes this by polling OpenSearch directly
(`GET /keycloak-events/_doc/<id>`, by the same `id` Vector's `id_key`
promotes to `_id`) after a successful forward, *before* committing chain
state or responding to Keycloak -- a short retry loop (`CONFIRM_RETRIES` /
`CONFIRM_DELAY_SECONDS`, default 10 attempts / 0.3s apart), since indexing
is fast but not synchronous with Vector's ACK. If the document still isn't
confirmed after that, the signer responds `502` exactly like a failed
forward -- chain state stays uncommitted, and Keycloak's own webhook retry
redoes the same link rather than the signer silently losing the event.

This uses a new, minimally-scoped OpenSearch user
(`opensearch/security-config/internal_users.yml`'s `audit_confirmer`,
mapped to the `keycloak_events_reader` role in `roles.yml` /
`roles_mapping.yml`) with only `indices:data/read/get` on
`keycloak-events*` -- read-only, get-by-id only, the same
"dedicated static internal user, minimally scoped" pattern already used
for `vector_writer`'s write-only access.

### Idempotent retries

Keycloak retries a webhook delivery on any non-2xx response, with backoff.
The retried delivery carries the *same* event `id` (only the extension's
own per-attempt `uid` differs). `audit_chain_signer.py` keeps a bounded
recent-ids map (last 500) and reuses a previously-assigned chain link for a
repeated `id` instead of advancing the chain again — otherwise a harmless
retry would look like a second, permanently-duplicated entry.

### A concurrency bug found and fixed during testing

An early version locked only the in-memory chain computation, forwarding to
Vector *outside* the lock and persisting state afterward in a second,
separate lock acquisition. A hash chain is inherently sequential — each
link depends on the previous one having actually been persisted — so two
webhook deliveries handled concurrently by `ThreadingHTTPServer` could both
read the same stale `prev_hash`/`seq` before either had persisted its
result. This wasn't theoretical: it produced a duplicate `audit_seq` and a
broken chain link during `setup.py`'s own provisioning burst, caught by
`scripts/verify_audit_chain.py` itself. Fixed by making the entire
read-compute-forward-persist cycle one atomic critical section — slower
under concurrency, but correctness here requires strict serialization
anyway.

## Verification

```bash
python3 scripts/verify_audit_chain.py
```

Fetches every chained document paginated via `search_after` on `audit_seq`
(1,000 at a time), not a single capped request -- an earlier version used a
single `size: 10000` search, which would have silently stopped verifying
past the 10,000th event with no indication it had. A verifier that can
silently stop verifying is worse than one that errors loudly, so this
always walks the full chain regardless of its length.

Walks every chained document in `audit_seq` order and checks, independently:
1. **Sequence contiguity** — a gap means a document was deleted.
2. **Chain linkage** — this document's `audit_prev_hash` must equal the
   previous document's `audit_hash`.
3. **`audit_raw_body` integrity** — its hash must still match `audit_hash`.
4. **Field consistency** — every top-level field must still match what's
   embedded in `audit_raw_body`.

Any failure names the exact `audit_seq` and what's wrong, not just "tampering somewhere."

**Manually inducing and then detecting tampering** (what was actually run
to validate this, not just assumed to work):

```bash
# Find a document's real id and tamper with a visible field directly,
# leaving audit_raw_body untouched:
curl -k -u admin:<OPENSEARCH_INITIAL_ADMIN_PASSWORD> \
  -X POST "https://localhost:9200/keycloak-events/_update/<doc_id>" \
  -H 'Content-Type: application/json' \
  -d '{"doc":{"authDetails":{"username":"root"}}}'

python3 scripts/verify_audit_chain.py
# -> TAMPERING DETECTED: field tampered at audit_seq=N: 'authDetails' is
#    {...'username': 'root'...} but audit_raw_body has {...'username': 'admin'...}
```

## What this doesn't cover (honest scope limits)

- **The `admin` credential itself is still a single point of trust.** If an
  attacker has it, they can tamper with a document *and* run
  `verify_audit_chain.py` themselves to confirm they got away with it
  before anyone else checks. This POC doesn't implement Level 2 (periodic
  Merkle-tree checkpoints published somewhere the attacker can't also
  reach — the Certificate-Transparency-style external anchor) — that's the
  real production answer, flagged here rather than silently assumed solved.
- **`audit_chain_signer.py`'s own state file is a single point of failure**
  for the chain's *continuation* (not its verifiability) — if it's lost
  without a backup, the chain doesn't break retroactively, but a fresh
  chain starts over from genesis, which itself is a fact verification
  can't distinguish from "this is a brand new deployment" unless someone
  is watching for it.
- **This only covers `keycloak-events`.** `audit_chain_signer.py`'s own
  logs, and OpenSearch's own audit log if `logWebhookEvents` were enabled
  (see phasetwo's `ext-event-webhook` docs), aren't chained.
