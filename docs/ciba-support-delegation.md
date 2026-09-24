# Support-delegated access via CIBA

A support agent can request authorization from a specific end user, and
once (and only once) that user approves, the support tool receives a
normal, user-scoped OIDC token it can use to act on their behalf — without
ever holding or seeing the user's password. This is OIDC **CIBA** (Client
Initiated Backchannel Authentication): a decoupled flow where the client
(here, `support-tool`) starts the authentication out-of-band, and the end
user completes it on their own device/session.

## Why this needed a spike first

Keycloak's CIBA implementation is real and works, but the actual wire
contract for the end-user-notification step is not consistently documented
across sources (a 2019 community design doc describes only a draft,
poll-only version predating ping/push; various blog posts describe the
*behavior* but not the literal request/response shapes). Per this project's
own [gotchas.md](gotchas.md) convention of confirming things empirically
rather than assuming from docs, every detail below was confirmed directly
against this project's actual `quay.io/phasetwo/phasetwo-keycloak:26.6.6`
container — by extracting and reading the real
`org.keycloak.protocol.oidc.grants.ciba.*` class files out of
`keycloak-services-26.6.6.jar` (`unzip`/`strings`, both present in the
image), and by firing real requests against a throwaway client and a
throwaway HTTP listener.

## The moving pieces

```
support_console.py --(1)--> Keycloak --(2)--> ciba_notifier.py
                                                     |
                                                (3) shows banner in
                                                     product-app
                                                     |
                                              user clicks Approve/Deny
                                                     |
                              Keycloak <--(4)-- ciba_notifier.py
support_console.py <--(5)-- Keycloak (poll)
```

1. **`support_console.py`** starts a backchannel auth request:
   `POST /realms/platform/protocol/openid-connect/ext/ciba/auth`
   (client-authenticated as `support-tool`), body:
   `scope=openid&login_hint=<user>&binding_message=<short-no-spaces-reason>`.
   Returns `{"auth_req_id": "...", "expires_in": 120, "interval": 5}`.
   `auth_req_id` is itself an encrypted JWT (opaque to the client either
   way — never decode it, just pass it back verbatim).

2. Keycloak's built-in **`ciba-http-auth-channel`** provider (SPI
   `ciba-auth-channel`) POSTs the pending request to whatever URL the
   `keycloak` container's
   `KC_SPI_CIBA_AUTH_CHANNEL_CIBA_HTTP_AUTH_CHANNEL_HTTP_AUTHENTICATION_CHANNEL_URI`
   env var points at — **`ciba_notifier.py`** here (`docker-compose.yml`).
   This is the one piece of CIBA Keycloak deliberately ships dev-only: in
   production you'd point this at a real "decoupled auth server" (push
   notification backend, SMS gateway, etc.); this project's
   `ciba_notifier.py` plays that role for the demo.

   Confirmed request shape:
   ```
   POST <httpAuthenticationChannelUri>
   Authorization: Bearer <JWT>          # opaque correlation token, see below
   Content-Type: application/json

   {"scope": "openid roles profile email", "binding_message": "...",
    "login_hint": "...", "is_consent_required": false}
   ```
   **Must respond `201 Created`** (body doesn't matter) — anything else
   makes Keycloak treat the whole notification as failed, which makes the
   *original* `/ext/ciba/auth` call from step 1 fail with a `503
   server_error` ("Failed to send authentication request"). Confirmed by
   testing without any listener configured first, then with one.

   The bearer JWT is a normal signed access-token-shaped JWT
   (`{"exp", "iat", "jti", "iss", "aud", "sub", "typ": "Bearer", "azp"}`) —
   `sub` is the target user's ID, `azp` is `support-tool`. There is **no**
   separate `auth_result_id` claim despite that name appearing in the
   decompiled class files — the request is actually correlated by the
   token's own `jti`. Practically this doesn't matter: `ciba_notifier.py`
   never inspects the token's claims at all, it just holds onto the raw
   token string and echoes it back verbatim in step 4.

3. **`ciba_notifier.py`** stores the pending request keyed by `login_hint`
   and exposes `GET /pending/<login_hint>`. `app/product_app.py`'s
   `index()` polls this (using the logged-in user's `preferred_username`
   claim — which is what `login_hint` resolution actually matches against,
   see below) and renders an "Support is requesting access — Approve /
   Deny" banner when there's a hit.

4. Clicking Approve/Deny hits `product-app`'s
   `/support-approval/{approve,deny}` routes, which call
   `ciba_notifier.py`'s matching endpoint, which relays the decision to
   Keycloak:
   ```
   POST /realms/platform/protocol/openid-connect/ext/ciba/auth/callback
   Authorization: Bearer <the exact same JWT from step 2>
   Content-Type: application/json

   {"status": "SUCCEED"}      # approve
   {"status": "CANCELLED"}    # deny (also valid: "UNAUTHORIZED")
   ```
   Returns `200` either way — the body isn't meaningful. Keycloak decodes
   the bearer token itself to find the pending request; `ciba_notifier.py`
   never generates or forwards any identifier of its own.

5. **`support_console.py`** polls
   `POST /realms/platform/protocol/openid-connect/token` with
   `grant_type=urn:openid:params:grant-type:ciba&auth_req_id=...`
   (client-authenticated as `support-tool`), handling:
   - `400 {"error": "authorization_pending"}` — keep polling at `interval`.
   - `400 {"error": "slow_down"}` — back off (interval += 5s), same as the
     OAuth2 device-flow convention CIBA reuses.
   - `400 {"error": "access_denied", "error_description": "not authorized"}`
     — after a deny.
   - `200` — a full token set (`access_token`/`refresh_token`/`id_token`),
     scoped to the *target user*, issued to the `support-tool` client
     (`azp`).

   `support_console.py` then calls the same self-scoped
   `GET /realms/platform/orgs` `app/product_app.py`'s `my_orgs()` uses,
   with the delegated access token, and prints the result — proof the
   action ran as the user via a token that only exists because they
   approved it.

## Configuration that made this work

- **Realm CIBA policy** (`provision_ciba_support.py`): Keycloak's own
  defaults on this image were *already* exactly what this demo needs —
  confirmed by reading `platform`'s realm representation back before this
  script ever touched it: `cibaBackchannelTokenDeliveryMode: "poll"`,
  `cibaExpiresIn: "120"`, `cibaInterval: "5"`,
  `cibaAuthRequestedUserHint: "login_hint"`. Set explicitly anyway so this
  stays correct if a future Keycloak upgrade ever changes its defaults.

- **`support-tool` client attributes**
  (`oidc.ciba.grant.enabled: "true"`, `backchannel.token.delivery.mode:
  "poll"`): not consistently documented by name anywhere — confirmed by
  creating a throwaway client with these exact attributes and firing a
  real backchannel auth request against it (got back a real `auth_req_id`,
  not `unauthorized_client`).

- **`login_hint` resolution**: matches the target user's *username* in the
  `platform` realm, not necessarily their email as a separate lookup. For
  every federated user in this project (`alice@org1/org2/org3/acme/globex
  .example.com`) the platform-realm username *is* their email (confirmed
  via the admin API), so passing the email works — but a federated user's
  platform-realm account doesn't exist until their first login (same
  gotcha as [multi-env-tenants.md](multi-env-tenants.md)), so
  `support_console.py <email>` will fail with `invalid_user` for someone
  who has never logged in yet. `shared.user` (a local user) resolves by
  its literal username, `shared.user`, not its email.

- **`binding_message` charset**: confirmed from Keycloak's own validation
  error text (read directly out of the class file) that it must be ≤50
  characters and reject spaces. `support_console.py` uses a short
  hyphenated message (`org-profile-access`) rather than a sentence.

## Trying it

There are two ways to play "support" -- a CLI script, or a browser UI. Both
drive the exact same Keycloak endpoints; neither is more "real" than the
other.

**CLI (`scripts/support_console.py`):**
```bash
# 1. Log in as a user who already has a platform account, e.g.:
#    http://acme.sample.127.0.0.1.nip.io:3000 as alice@acme.example.com
#    -- leave the tab open.

# 2. In a separate terminal, play "support":
python3 scripts/support_console.py alice@acme.example.com

# 3. Refresh the product-app tab -- an approval banner appears.
#    Click Approve (or Deny).

# 4. support_console.py either prints alice's org memberships (approved)
#    or "request denied or failed: access_denied" (denied).
```

**Browser UI (`support_console_app/`, `http://localhost:3001`):**
```
1. Log in as above and leave the product-app tab open.
2. In a second tab, open http://localhost:3001, enter the user's email
   and a reason, click "Request access".
3. Refresh the product-app tab -- approve (or deny).
4. The support-console tab updates on its own (polling in the background,
   same interval/backoff Keycloak told it to use) and shows the decoded ID
   token, decoded access token, and org memberships once approved -- or
   "Denied by the user." otherwise.
```

`support_console_app.py` is deliberately stateless: the only thing
identifying a pending request is Keycloak's own `auth_req_id`, carried in
the `/wait` page's URL and JS, not a server-side session. `/poll` makes one
token-endpoint attempt and returns immediately (a blocking loop like the
CLI script's doesn't work inside a single browser request); the page's own
JavaScript is what re-calls it on an interval until the request resolves or
expires.

## Expiry

`ciba_notifier.py` evicts a pending entry once it's older than
`PENDING_TTL_S` (130s, just past the realm's 120s `cibaExpiresIn`) — an
un-actioned CIBA request is already gone from Keycloak's side by then (its
device-code model expired), so any later approve/deny would just `502`
forever with no way to clear the banner. Confirmed live during testing:
leaving too long a gap between starting `support_console.py` and clicking
Approve reproduced exactly this stuck state before the TTL eviction was
added.

## Scope of this demo

Deliberately read-only: `support_console.py` only calls the self-scoped
`GET /realms/platform/orgs` — never a mutating action — since the point
being demonstrated is *delegated, user-approved access*, not what a support
tool is allowed to do with it once obtained. A real integration would scope
the `support-tool` client down further (a dedicated client scope /
audience, not the default `openid roles profile email`) before granting it
anything beyond read access.
