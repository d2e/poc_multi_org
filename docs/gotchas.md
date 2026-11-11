# Gotchas encountered while building this (for future reference)

## Multi-environment tenants (`globex`)

- **A federated user's platform account doesn't exist until their first
  login** — pre-granting `globex-stage`/`globex-prod` membership+roles
  during `setup.py` needs alice's `platform`-realm user ID first, but she's
  a federated (not local) user, same as org1/2/3/acme's users. Solved by
  having `provision_globex_tenant.py` drive one real login itself, reusing
  the browser-simulation machinery `verify.py` already had (now shared via
  `scripts/browser_login.py`). See
  [multi-env-tenants.md](multi-env-tenants.md).
- **Active org is a persisted user attribute (`org.ro.active`), not
  "always defaults to the first org."** It survives across logins. This
  broke `verify.py`'s own multi-env test during development: after manually
  testing the switch flow by hand, a subsequent *fresh* automated run found
  alice's active org already on whatever was last switched to, not
  `globex-dev`. Fixed by having the test explicitly switch to a known
  starting point right after login instead of asserting on whatever a
  fresh login happened to restore.
- **`keycloak-orgs`' org-scoped role API had never been used in this repo**
  before this feature (confirmed by reading the actual extension source,
  not docs — the published docs' endpoint names
  (`/api/create-organization-role/` etc.) turned out to be documentation-
  generator placeholder slugs, not real paths). The real REST shape, from
  `RoleResource.java`/`RolesResource.java` in
  [p2-inc/keycloak-orgs](https://github.com/p2-inc/keycloak-orgs):
  `POST /realms/{realm}/orgs/{orgId}/roles` to define a role,
  `PUT .../roles/{roleName}/users/{userId}` to grant it.
- **Multiple `post.logout.redirect.uris` values need `##` as the
  separator** — not a JSON array, not comma-separated, not newline (this is
  a single-string client attribute, unlike `redirectUris`/`webOrigins`
  which are real list fields). Confirmed by actually driving a full logout
  through both tenants' origins after setting it, not just by reading a
  forum answer. `provision_product_app_client.py`'s `TENANT_URLS` list
  needs both `acme`'s and `globex`'s origins joined this way in one
  attribute value.
- **`switch-organization`'s token response carries a different `sid` than
  the browser's actual SSO session** — confirmed by decoding both: logging
  in gives one `sid`, switching orgs even once gives back a token with a
  *different* one, despite no re-authentication happening. `app/product_app.py`
  was overwriting the session's `id_token_raw` on every switch (via the
  shared `store_tokens()`), so `/logout`'s `id_token_hint` after any switch
  no longer matched the real session Keycloak needed to end — reported as
  "switch org, then log out, and the session doesn't end." RP-initiated
  logout doesn't error in this case, it just silently no-ops (`200`, no
  redirect chain) since the hint doesn't correspond to any active session.
  Fixed by pinning a separate `login_id_token_raw` at `/callback` (never
  touched by `switch()`) and using that for the logout hint instead. See
  [tenant-routing.md](tenant-routing.md).
- **The obvious test for "did logout end the SSO session" can't actually
  fail, whatever the real answer is.** `GET /` after `/logout` always
  redirects to `/login` regardless of whether Keycloak's SSO session
  survived — the *local* Flask session is unconditionally cleared by
  `/logout` either way. `scripts/verify.py`'s original logout check
  (`status_code == 200 and "Welcome" in text`) was structurally incapable of
  catching the bug above, which is exactly how it shipped unnoticed: the
  check always reported PASS. Fixed with `sso_session_ended()`, which
  follows `/login`'s own redirect chain and checks whether it's forced
  through a real password form (SSO genuinely ended) versus silently
  re-authenticating without one (SSO still alive) — the only signal that
  actually distinguishes the two cases. Confirmed the new check is real by
  deliberately reverting the fix locally and watching it fail, then
  restoring the fix and watching it pass — don't trust a regression test
  that's never been observed to fail.

## Keycloak / phasetwo

- **Port 8080 was already taken** on this machine by another local dev
  service, so this POC binds Keycloak to **8082** instead.
- **Keycloak 26 ships its own *native* Organizations feature**, separate from
  phasetwo's `keycloak-orgs` extension used here. The default `browser` flow
  already contains a conditional "Organization Identity-First Login" branch
  for the native feature — `scripts/provision_browser_flow.py` explicitly
  disables it so it doesn't fight with phasetwo's Home IdP Discovery
  authenticator.
- **Reordering authentication executions**: after adding a new execution via
  `POST .../executions/execution`, it's appended at the end of the subflow.
  Use `raise-priority` / `lower-priority` to move it. When later setting
  `.requirement` via `PUT .../executions`, you must PUT the **full** execution
  representation (fetch it, mutate just `.requirement`, PUT it back) — sending
  a partial `{id, requirement}` body silently resets ordering and breaks the
  flow (manifested as a cryptic `invalid_user_credentials` error thrown by the
  Password Form authenticator when it ran before Home IdP Discovery had set a
  user in context).
- **Home IdP Discovery (`ext-auth-home-idp-discovery`) replaces the
  *combined* Username Password Form**, not the whole flow: it only collects
  and resolves the username. It must be immediately followed by a standalone
  `auth-password-form` execution to actually collect the password.
- **Linking a second IdP to an org doesn't error — it silently evicts the
  first one.** `POST /realms/{realm}/orgs/{orgId}/idps` returns `201` every
  time, but with the org config's `multipleIdpsEnabled` off (the default),
  it also clears the *previous* IdP's `home.idp.discovery.org` attribute —
  no exception, no 409, nothing in the response to signal it. Confirmed by
  decompiling `IdentityProvidersResource.createIdentityProvider()` in the
  actual `keycloak-orgs` jar: it calls
  `deactivateOtherIdps(rep, !isMultipleIdpsConfigEnabled(realm), orgId)`.
  Turn `multipleIdpsEnabled` on (`PUT /realms/{realm}/orgs/config`) *before*
  linking a second IdP to the same org, or the first one quietly stops being
  reachable via Home IdP Discovery. Even then, precedence between two
  genuinely-linked IdPs on the same domain is implementation-defined (not a
  configurable priority) unless each IdP also declares its own
  `home.idp.discovery.domains` to disambiguate. See
  [dual-idp-precedence.md](dual-idp-precedence.md) for the full,
  empirically-verified writeup.
- **Deleting an org does not delete its linked IdPs.** They remain as
  ordinary (if now un-linked, `home.idp.discovery.org` cleared) realm-level
  identity providers — confirmed while tearing down and re-provisioning the
  dual-IdP test scenario: re-running `provision_dual_idp_scenario.py` after
  deleting just the org 409'd trying to re-create an IdP alias that was
  "gone" from the org but still very much present at
  `/admin/realms/{realm}/identity-provider/instances/{alias}`. A full
  cleanup of an org's IdPs needs an explicit second delete call against that
  endpoint, not just deleting the org.
- Domains are considered unverified by default
  (`requireVerifiedDomain: false` on the Home IdP Discovery authenticator, and
  no domain TXT verification is run) — fine for a local POC, but a production
  setup should verify domains via `POST /{realm}/orgs/{orgId}/domains/{domain}/verify`.
- **Keycloak's own Docker healthcheck is broken**: it relies on a bash-only
  `/dev/tcp/...` redirection, but `CMD-SHELL` runs under `/bin/sh`, so it
  always reports `unhealthy` even though the app itself starts fine. This
  went unnoticed for a while because nothing depended on it — readiness is
  polled at the application level instead (`wait_for_keycloak()` in
  `keycloak_admin.py`). `org-cache-watcher` is the first thing that would
  have gated on it (`depends_on: condition: service_healthy`); it uses
  `condition: service_started` plus its own retry loop instead. **Now
  actually fixed** (see [opensearch-auth.md](opensearch-auth.md)) — it had
  a *second*, separate bug on top of the `/dev/tcp`-under-`/bin/sh` one:
  it was hitting `/health/ready` on port 8080, but Keycloak 26's health
  endpoints live on a separate management interface, port 9000 by default.
  This started mattering once `opensearch-dashboards` needed a real
  `service_healthy` gate on Keycloak (its OIDC plugin crashes fatally, no
  retry, if Keycloak isn't actually up yet).
- **Keycloak has no "get flow by alias" endpoint**: `GET
  .../authentication/flows/{alias}` 404s unconditionally regardless of
  whether a flow with that alias exists (it wants the flow's UUID, not its
  alias). `provision_browser_flow.py`'s idempotency check used this path and
  always got 404, so every re-run past the first tried to re-copy the
  `browser` flow and hit a 409. Fixed by listing
  `.../authentication/flows` and checking by alias instead.
- **`post.logout.redirect.uris` rejects all post-logout redirects by
  default when unset** — it does not fall back to `redirectUris` the way
  you might expect. RP-initiated logout (`product-app`'s `/logout`)
  propagates upstream through a federated login chain (`platform` →
  `acme-idp` → back to `platform`'s broker callback → the app), so **two**
  separate clients need this attribute set, not just the app-facing one:
  `product-app` itself, and `platform-broker` in **every** `{org}-idp`
  realm. Missing it on the broker client fails logout partway through with
  "Invalid redirect uri" — on a client you wouldn't naturally suspect. See
  [tenant-routing.md](tenant-routing.md).
- **Clearing an app's own session cookie is not the same as logging out of
  Keycloak.** Keycloak's SSO session stays active, so revisiting the login
  route silently re-authenticates via SSO — looks exactly like logout did
  nothing. Needs real RP-initiated logout (Keycloak's `end_session`
  endpoint with `id_token_hint`), not just clearing the local cookie.

## OpenSearch / Vector / event pipeline

- **OpenSearch's docker image needs `DISABLE_SECURITY_PLUGIN=true`**, not
  `plugins.security.disabled=true` as an environment variable directly —
  setting both at once causes a startup error (`setting
  [plugins.security.disabled] already set`), since the entrypoint script
  translates the former into the latter itself.
- **`opensearchproject/data-prepper`'s HTTP source only accepts JSON
  *arrays*** per request; the phasetwo webhook always POSTs a single JSON
  object. Tried first for ingestion, replaced with
  [Vector](https://vector.dev/) instead — its `http_server` source +
  `json` codec accepts either shape. See [event-logging.md](event-logging.md).
- **A single-file Docker bind mount can't be `rename()`d onto**: the mount
  target itself is a mount point, so `os.replace()` (the usual
  write-temp-then-rename atomic-write pattern) fails with `EBUSY`. Fixed by
  bind-mounting the containing directory (`./opensearch:/data`) instead of
  the single CSV file, so the file inside it is an ordinary, renameable file.
- **Vector's `--watch-config` file watch tracks the enrichment table's
  inode**, not just its path: a rename-based atomic write swaps in a new
  inode, so the watch only ever fires once — every write after the first
  silently goes unnoticed (confirmed: a newly created org resolved to a
  `null` `org_name` even though the CSV on disk was correct). `write_csv()`
  in `org_cache_watcher.py` writes in place (open-truncate-write, same
  inode) instead, at the cost of a (here negligible) atomicity window.
- **Provisioning order matters**: the org-cache-watcher's webhook must be
  registered *before* any org is created, or Keycloak fires (and loses) the
  creation event before anything is listening. See
  [event-logging.md](event-logging.md).
- **OpenSearch's dynamic mapping stores strings as `text` by default**, with
  an auto-added `.keyword` sub-field for exact-match/aggregations. A `terms`
  aggregation on `type` (not `type.keyword`) fails with
  `illegal_argument_exception`.

## OpenSearch / OpenSearch Dashboards authentication (Keycloak-backed)

See [opensearch-auth.md](opensearch-auth.md) for the full writeup — several
non-obvious, code-verified-against-the-actual-2.19.1-source gotchas live
there: lowercase-only action group names (`create_index`/`write`, not
`CREATE_INDEX`/`WRITE`), `roles_key`'s flat-claim-only extraction (no
dotted paths), `base_redirect_url` needing to be set explicitly,
`multiple_auth_enabled` being required (not optional) once `auth.type` is
an array, Dashboards' OIDC plugin crashing fatally at boot if Keycloak
isn't fully ready yet, the webhook signature's exact (undocumented)
encoding, and Vector's env-var interpolation being disabled by default
with no error when it silently doesn't substitute.

## Audit log tamper detection (`keycloak-events` hash chain)

See [audit-log-integrity.md](audit-log-integrity.md) for the full writeup.
Two real bugs found while building it, both worth knowing about generically:
- **The `write` action group also grants delete/update** (it expands to
  `indices:data/write*`) — a role meant to be append-only silently wasn't.
  The bulk API's *actual* permission check is against a shard-level
  sub-action (`indices:data/write/bulk[s]`), not the bare action name, so
  the fix (explicit actions instead of the group) needs wildcards, not just
  a shorter allow-list, or it 403s every real write while looking like it
  worked (delete/update correctly still blocked).
- **Verifying a stored hash against a redundant raw-content field is not
  the same as verifying the document itself.** An early version of
  `verify_audit_chain.py` only checked that `audit_raw_body`'s own hash
  still matched — a live test tamper of a *different*, human-visible field
  (`authDetails.username`) went completely undetected, since nothing
  compared the two. The check that actually matters is cross-referencing
  every visible field against what the raw content says it should be.
- **A hash chain's compute-then-persist step must be one atomic critical
  section**, not two separately-locked ones with an external network call
  in between — a narrower lock let concurrent webhook deliveries read the
  same stale "previous hash" before either persisted, producing a
  duplicate sequence number and a broken chain link during a real
  provisioning burst, not a hypothetical race.

## OpenSearch Dashboards

- **`searchSourceJSON.index` must not be a literal index-pattern ID.**
  OSD/Kibana resolves it indirectly via `indexRefName` (a string naming a
  reference) plus a matching entry in the saved object's own `references`
  array. Inlining the literal ID directly silently breaks every panel with
  *no visible error on the page* — only in the browser console. Confirmed
  via a real headless-browser load (`playwright`), not just a schema guess.
- **Every dashboard panel needs a `version` field** in `panelsJSON`, or OSD's
  panel migration code throws `TypeError: Cannot read properties of null
  (reading 'version')` while loading — again, silent on the page itself.
- **A field missing from the index pattern's cached field list** (e.g. one
  that didn't exist in the index yet when the pattern was created) causes
  `Could not locate that index-pattern-field` on just that one panel/query,
  even though the field is genuinely in the live mapping. This recurred on
  a genuinely fresh `setup.py` run even after `provision_dashboards.py`
  already refreshed the field list once: it's the *last* provisioning step,
  but Vector's webhook→OpenSearch indexing still has a few seconds of lag
  behind the org-scoped events fired moments earlier — real, observed
  read-after-write lag, not a one-off fluke. `fetch_fields()` now retries
  (up to 15s) for the specific fields the dashboard's panels actually need
  before giving up, rather than a single fetch-and-hope. See
  [dashboards.md](dashboards.md).
- **An index pattern created via the UI wizard can silently fail to save.**
  Check `curl http://localhost:5601/api/saved_objects/_find?type=index-pattern`
  — `total: 0` means nothing actually persisted, regardless of what the
  wizard appeared to do.
- See [dashboards.md](dashboards.md) for the full troubleshooting writeups
  of all three.

## `verify.py` / driving Keycloak's real login flow with `requests`

- **`requests.Session`'s cookie jar can't be used to drive Keycloak's actual
  login HTML/redirect flow across two realms**: (1) requests silently
  re-derives a fresh, default-policy `CookieJar` on every outgoing request
  regardless of the session jar's policy, so a `Secure` cookie (which
  Keycloak sets even over plain http in dev mode) is accepted into storage
  but never sent back ("Restart login cookie not found"); (2) it isn't
  path-scoped the way `curl`'s jar or a real browser are, so same-named
  session cookies set by two different realms at different paths
  (`/realms/platform/` vs `/realms/org3-idp/`) clobber each other.
  `verify.py`'s `PlainCookieJarSession` tracks cookies per-path by hand
  instead.
- **`requests`' cookie jar silently drops already-expired cookies from
  `response.cookies` entirely** rather than surfacing them as deletions —
  that's how a server says "delete this" (`Max-Age=0` / `Expires` in the
  past). A naive per-cookie loop over `resp.cookies` (what
  `PlainCookieJarSession` originally did) never sees these, so it keeps
  resending a stale cookie value forever after a real logout — which
  silently masks a genuine logout bug as "looks fine" in an automated test.
  Fixed by parsing the raw `Set-Cookie` headers directly and removing
  matching entries when `Max-Age=0` or the expiry is in the past.

## `app/product_app.py` / token lifecycle

- **A tab left open past the access token's lifetime 500s, not 401s
  gracefully.** Keycloak access tokens are short-lived by design (a few
  minutes); `my_orgs()` (used by both `/` and `/switch/<org_name>` to look
  up the user's org memberships) called Keycloak with the stale
  `access_token` from the session and let a 401 propagate up as an
  unhandled `requests.exceptions.HTTPError`, crashing the whole page to a
  500 — this is exactly what a real user hit revisiting a tab after a short
  idle period. Confirmed via `docker logs p2-product-app`, not guessed.
  Fixed by storing the `refresh_token` in the session too (`store_tokens()`)
  and having `my_orgs()` catch a 401, attempt one `refresh_access_token()`
  (the standard OAuth2 `grant_type=refresh_token` exchange), and retry once
  before giving up. `index()`/`switch()` catch the still-possible
  `requests.HTTPError` (refresh token itself also expired, or the Keycloak
  SSO session ended) and fall back to `session.clear()` + redirect to
  `/login` — a clean re-login prompt instead of a 500 page.
- **Verified by forging a tampered session cookie**, not just by reading the
  code: logged in for real to get a genuine `refresh_token`, then used
  Flask's own `itsdangerous` serializer (with the app's known dev secret) to
  swap in a deliberately invalid `access_token` while keeping the real
  `refresh_token`, and confirmed the page still rendered correctly (recovery
  via refresh). Repeating with *both* tokens invalid confirmed the clean
  fallback to `/login` rather than a 500.
- **A second, separate failure -- `MismatchingStateError` on `/callback`,
  and then `ERR_TOO_MANY_REDIRECTS`** -- turned out to be two layers of the
  *same* underlying bug, not two different bugs. First report ("logout then
  login and got 5xx") showed `authlib...MismatchingStateError:
  mismatching_state` in `docker logs p2-product-app`; catching
  `authlib.integrations.base_client.errors.OAuthError` around
  `authorize_access_token()` and redirecting to `/login` turned the 500 into
  a redirect -- which then surfaced the *real* problem as
  `ERR_TOO_MANY_REDIRECTS` instead, because the fix was papering over a
  persistent failure, not a transient one. The actual root cause, also
  sitting right there in the container logs the whole time:
  `UserWarning: The 'session' cookie is too large ... Browsers may silently
  ignore cookies larger than this` (Werkzeug's own warning, ~4130-4160 bytes
  against a ~4093-byte limit). The session was storing `id_token_raw` *and*
  a fully-decoded copy of the same JWT (`id_token_claims`) alongside
  `access_token` and `refresh_token` -- after a few active-organization
  switches grew `active_organization`'s claim payload, the signed cookie
  crossed the browser's cookie-size ceiling and got silently dropped. A
  dropped session cookie looks exactly like "logged out" to `index()`, so
  the very next request bounces to `/login`; since Keycloak's SSO session
  was still alive, that silently re-authenticates and produces another
  oversized (and thus also-dropped) cookie -- an infinite loop, not an
  occasional glitch, which is why redirecting-instead-of-500ing just traded
  one visible failure for another. Fixed by dropping `id_token_claims` from
  the session entirely and decoding claims **on demand** from
  `id_token_raw` instead (`current_claims()` in `app/product_app.py`) --
  eliminates the single largest duplicate, since a JSON-per-field
  representation of a JWT's payload is larger than the compact base64 JWT
  it was decoded from. Confirmed via repeated login + 9 active-org switches
  in a real browser: cookie stayed at ~2900-2940 bytes throughout (was
  4130-4160+ before), and the "cookie is too large" warning stopped
  appearing in the logs entirely. The `OAuthError` catch on `/callback`
  is kept as a secondary, genuinely-defensive measure -- it's still the
  right behavior for an actual stale/replayed callback URL (back button, a
  duplicate request) -- but it was never the fix for *this* bug, only a
  layer sitting on top of it. **Lesson**: when a fix for one 5xx immediately
  surfaces a different-looking failure, don't assume the first fix was
  wrong and the new symptom is a fresh, unrelated bug -- check whether
  they're two layers of the same root cause before diagnosing further, and
  check the *full* log output (warnings included, not just tracebacks) --
  Werkzeug had already named the real problem before the state-mismatch
  investigation even started.
- **The exact same `ERR_TOO_MANY_REDIRECTS` came back after the fix above
  shipped**, still with the same "cookie is too large" warnings in
  `docker logs p2-product-app`, at essentially the same size (~4130-4165
  bytes) as before. Two things were true at once here, and both needed
  fixing:
  1. **A session cookie issued by the previous build still had the removed
     `id_token_claims` key baked in.** Flask's client-side session is a
     cumulative dict persisted *inside the cookie itself* -- a still-open
     browser tab (or a tab that reloads a page from cache/back-forward
     navigation) keeps sending whatever cookie it already has, regardless
     of what the newly-deployed server code would write on a *fresh*
     login. "Stop writing a field" does not retroactively shrink a cookie
     a client already holds. Confirmed by forging a cookie with
     `id_token_claims` added back in (simulating a pre-fix browser) and
     showing that hitting `/switch/...` correctly shrank it back down
     afterward (3240 -> 2920 bytes) -- so `store_tokens()` now also calls
     `session.pop("id_token_claims", None)` unconditionally, not just
     stops writing it, specifically so an already-affected browser
     self-heals on its very next request instead of needing a manual
     cookie clear.
  2. **That alone still wasn't the full picture** -- removing one
     duplicate field is fundamentally whack-a-mole against a hard, fixed
     ceiling (~4093 bytes): the next thing that grows the token payload
     (more org attributes, a bigger `realm_access`/`resource_access`
     claim, more roles) hits the exact same wall again, just with a
     different field as the straw that broke it. The durable fix was
     architectural, not another field-level trim: **moved to server-side
     sessions** (`flask-session`, filesystem backend, `Session(app)` in
     `app/product_app.py`, volume-mounted at `/data/flask_session` in
     `docker-compose.yml` so a container restart doesn't invalidate every
     open session). The browser-facing cookie is now a fixed ~43-byte
     opaque session ID regardless of how much token data lives
     server-side -- confirmed via a real browser doing 18 rapid
     active-org switches in a row (cookie size never moved) and via a
     live `docker restart p2-product-app` mid-session (user stayed logged
     in afterward, proving the volume mount actually persists sessions
     across restarts, not just across requests). This closes off the
     entire *class* of bug, not just this instance of it.

## CIBA support-delegated access

See [ciba-support-delegation.md](ciba-support-delegation.md) for the full
writeup. Everything here was confirmed by extracting and reading the real
`org.keycloak.protocol.oidc.grants.ciba.*` classes out of this project's
actual `keycloak-services-26.6.6.jar` (`unzip`/`strings` are both present in
the `phasetwo-keycloak` image) and firing real requests, not assumed from
docs or blog posts:
- **The 2019 CIBA community design doc is stale and describes a
  poll-only draft** that predates ping/push and doesn't match the actual
  shipped endpoint paths (`/ext/ciba/auth`, `/ext/ciba/auth/callback`, not
  `/backchannelAuthn`/`ciba-decoupled-authn-callback`).
- **Keycloak ships no real end-user-notification mechanism for CIBA at
  all** — its one built-in "Authentication Channel" provider
  (`ciba-http-auth-channel`) just POSTs the pending request to a
  configurable URL and requires exactly a `201 Created` response, or the
  *client's* original `/ext/ciba/auth` call fails with a `503
  server_error` ("Failed to send authentication request") — confirmed by
  testing with no listener configured, then with one.
- **The notification's bearer token has no `auth_result_id` claim**
  despite that name appearing in the decompiled class files — correlation
  actually happens via the token's own `jti`, but this is moot for an
  integrator: never decode it, just hold the raw string and echo it back
  verbatim to the callback endpoint later.
- **`binding_message` silently rejects spaces** (confirmed from Keycloak's
  own validation error text, ≤50 chars, "basic plain-text characters
  without spaces") — a natural-language sentence will 400 with
  `invalid_binding_message`; use a short hyphenated token instead.
- **`login_hint` resolves against the target's `platform`-realm
  *username*, not a separate email lookup** — happens to be the same value
  for every federated user in this project (confirmed via the admin API:
  `alice@org3.example.com`'s platform username literally is
  `alice@org3.example.com`), but a federated user who has never logged in
  yet has no platform-realm account at all yet (same gotcha as
  [multi-env-tenants.md](multi-env-tenants.md)'s), so their `login_hint`
  fails with `invalid_user` until they log in once.

## `docker compose` / `product-app`'s build

`docker compose up -d` never rebuilds an image for a container that
already exists, even if `app/Dockerfile`'s build context (`app/
product_app.py`) changed since the image was last built -- confirmed while
verifying the CIBA support-approval banner (see
[ciba-support-delegation.md](ciba-support-delegation.md)): the new banner
code was live on disk, `setup.py` ran clean with no errors, but the
running container kept serving the pre-edit code with zero indication
anything was stale. `scripts/setup.py` now runs `docker compose up -d
--build` for exactly this reason -- `--build` is required to pick up
`app/`-directory edits on a re-run, not just for a from-scratch `docker
compose down -v`.

## macOS / Colima

OpenSearch requires `vm.max_map_count >= 262144` on the host. If it fails to
start with a `max virtual memory areas` error, run:
```bash
colima ssh -- sudo sysctl -w vm.max_map_count=262144
```
(this doesn't persist across Colima VM restarts — re-run it if needed).
