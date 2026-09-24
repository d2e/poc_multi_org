# Subdomain-based IdP routing (tenant "acme")

## What this demonstrates

The existing multi-org setup routes users to their federated IdP based on
the **email domain they type in** (Home IdP Discovery:
`alice@org1.example.com` → `org1-idp`). This feature demonstrates a
different, equally common enterprise SSO pattern: routing based on the
**subdomain a tenant's product URL is hosted on**, known before the user
types anything.

A user belonging to tenant "acme" hits `acme.sample.127.0.0.1.nip.io:3000`
(their product's tenant-scoped URL, standing in for a real
`acme.sample.com`). If they have no session, the app sends them straight to
`acme`'s federated IdP — no email prompt, no IdP picker.

```
Browser -> product-app (Flask, app/product_app.py)
  no session -> redirect to Keycloak /auth with kc_idp_hint=acme
    -> identity-provider-redirector authenticator matches "acme" IdP alias
       -> redirects straight to acme-idp's login page (no email prompt)
    -> user logs in as alice@acme.example.com
    -> broker callback, post-org-broker-login auto-adds membership to "acme" org
    -> redirect back to product-app/callback with an auth code
  product-app exchanges the code, decodes the ID token,
  shows the active_organization claim (Keycloak's own answer to
  "which org is this"), which should match the URL's tenant.
```

## Why no Keycloak flow surgery was needed

Inspecting the `org discovery browser` flow (copied from stock `browser`
when this project was first built) showed the
**`identity-provider-redirector`** authenticator is already present, as a
top-level `ALTERNATIVE` step, positioned *before* our custom Home IdP
Discovery forms subflow:
```
0 ALTERNATIVE  Cookie
0 DISABLED     Kerberos
0 ALTERNATIVE  Identity Provider Redirector   <-- this
0 ALTERNATIVE  org discovery browser forms    <-- Home IdP Discovery + Password Form
```
This authenticator reads the standard `kc_idp_hint` query parameter on the
`/auth` request:
- If it matches a real IdP alias in the realm, it redirects straight there,
  short-circuiting the rest of the ALTERNATIVE list.
- If absent (or invalid), it returns "attempted" and falls through to the
  next ALTERNATIVE — the existing email-based flow, **unchanged**.

So `org1`/`org2`/`org3`'s existing behavior is untouched; `acme` just
exercises a code path that was already wired in but never used.

## Two distinct "domain" concepts — don't confuse them

- **`acme.example.com`** — the tenant's *email domain*, used by Home IdP
  Discovery (existing mechanism, e.g. a user could still type
  `alice@acme.example.com` on `platform`'s login page and get routed the
  old way).
- **`acme.sample.127.0.0.1.nip.io`** — the tenant's *product-app subdomain*,
  used by `kc_idp_hint` routing (this feature, resolved before the user
  types anything).

They happen to map to the same org/IdP alias (`acme`) in this demo, but
conceptually they're independent — a real deployment could have completely
different naming for each.

## Local subdomain resolution: nip.io

`acme.sample.127.0.0.1.nip.io` relies on [nip.io](https://nip.io), a public
wildcard DNS service that resolves any `*.<ip>.nip.io` to `<ip>` — so this
"just works" for a real browser with normal internet access, no
`/etc/hosts` edit (which would need `sudo`) required.

**Caveats**:
- Needs internet access for the DNS lookup itself — doesn't work fully
  offline, or in network-restricted environments. (Confirmed while building
  this: nip.io, and other equivalent services like `localtest.me`, don't
  resolve at all from this project's sandboxed tool environment, even
  though normal domains resolve fine there — looked like a deliberate
  block on localhost-resolving DNS tricks, not a general outage. Untested
  whether that's specific to that sandbox or a broader pattern; if you hit
  the same thing, fall back to an `/etc/hosts` entry:
  `echo "127.0.0.1 acme.sample.127.0.0.1.nip.io" | sudo tee -a /etc/hosts`.)
- `scripts/verify.py`'s automated test doesn't rely on nip.io resolving at
  all — it connects directly to `127.0.0.1:3000` and sets the `Host` header
  explicitly (see `_tenant_get()`), which is both more portable and works
  offline. Only the manual, real-browser walkthrough needs nip.io (or the
  `/etc/hosts` fallback) to actually work.

## Adding more tenants

Keycloak doesn't support host-wildcard redirect URIs — only exact matches
or a trailing path `*` — so `product-app`'s `redirectUris` needs one
literal entry per tenant subdomain
(`scripts/provision_product_app_client.py`). To add a second tenant:
1. Add an entry to `TENANTS` in `scripts/keycloak_admin.py`.
2. Add its subdomain → IdP alias mapping to `TENANT_IDP_ALIAS` in
   `app/product_app.py`.
3. Add its redirect URI to `provision_product_app_client.py`.

## The `KC_URL` / `KC_INTERNAL_URL` split, again

Same pattern as everywhere else in this project (see
[architecture.md](architecture.md)) — `app/product_app.py` registers its
OAuth client with **explicit** endpoints rather than OIDC discovery,
because discovery would only give one base URL and this app needs two:
`authorize_url` (browser-facing, `KC_URL`) vs `access_token_url` /
`userinfo_endpoint` / `jwks_uri` (server-to-server, `KC_INTERNAL_URL`).
Forgetting `jwks_uri` specifically breaks the callback with
`RuntimeError: Missing "jwks_uri" in metadata` — Authlib's
`authorize_access_token()` validates the ID token signature itself and
needs it to fetch the signing keys.

## Logout: app session vs Keycloak SSO session

Clearing `product-app`'s own Flask session cookie in `/logout` is **not**
enough on its own — Keycloak's SSO session (and the browser's cookies for
it) stays active, so the very next visit to `/` → `/login` silently
re-authenticates via SSO and lands the user right back in, looking exactly
like logout did nothing. `/logout` does proper **RP-initiated logout**
instead: redirects to Keycloak's `end_session` endpoint with
`id_token_hint` and `post_logout_redirect_uri` back to the app.

**The `id_token_hint` must be the token from the actual login, not the
latest one.** `switch()` calls `store_tokens()` on every active-org switch,
which overwrites `id_token_raw` with the switch-organization endpoint's
fresh token set -- confirmed empirically that this fresh token carries a
**different `sid`** (session ID claim) than the one the browser's actual
Keycloak SSO session is running under, even though no re-authentication
happened. RP-initiated logout matches `id_token_hint` against a real
session by its `sid`; handing it a switch-minted token doesn't match
anything, so Keycloak silently no-ops (returns `200`, no redirect chain)
instead of ending the session -- which is exactly what "switch org, then
log out, and the session doesn't end" reported. Fixed by pinning a
**separate** `login_id_token_raw` in the session at `/callback` (the actual
authentication event) that `switch()` never touches, and having `/logout`
use that instead of the display-facing `id_token_raw`.

This one slipped through review for a while because the natural-looking
test assertion -- "does `GET /` come back with `200` and show `Welcome`
after logout" -- **can never fail**, whether or not Keycloak's SSO session
actually ended: `/logout` always clears the *local* Flask session
regardless, so `/` always `302`s to `/login` either way. `sso_session_ended()`
in `scripts/verify.py` fixes this by actually following `/login`'s redirect
chain and checking whether it's forced to show a real password form
somewhere along the way, or silently re-authenticates without one --
confirmed to actually discriminate the bug by reverting the fix locally and
watching the check fail, then restoring it and watching it pass.

This surfaced a real config gap, not just an app bug: RP-initiated logout
here **propagates upstream** — `platform`'s `end_session` redirects to
`acme-idp`'s own `end_session` too (federated logout), which redirects back
to `platform`'s broker callback (`/realms/platform/broker/acme/endpoint/logout_response`)
before finally landing back on the app. That means **two** clients need a
valid `post_logout_redirect_uri`, not just `product-app`:
1. `product-app` (in `platform`) — needs the app's own URL.
2. `platform-broker` (in **every** `{org}-idp` realm, including `acme-idp`)
   — needs `{KC_URL}/realms/platform/broker/{org}/endpoint/*`, since that's
   where the federated logout chain redirects back to.

Keycloak's `post.logout.redirect.uris` client attribute rejects **all**
post-logout redirects by default when unset — it does not fall back to
`redirectUris` the way you might expect. Without it on `platform-broker`,
logout fails partway through with **"Invalid redirect uri"**, on a
different client than the one you'd naturally suspect. Both
`provision_idp_realms.py` and `provision_product_app_client.py` now set
this attribute.

Separately, every response from `app/product_app.py` sends `Cache-Control:
no-store` (see the `no_cache` `after_request` hook) — every page here is
session-specific, so nothing should ever be cached. This matters
specifically for the browser's back/forward cache (bfcache): without
`no-store`, hitting **Back** right after `/logout` can restore the
*previously rendered* page straight from bfcache without re-requesting it
at all, showing "Welcome" again even though the session was genuinely
cleared server-side — looks exactly like "logout did nothing," but is a
stale client-side render, not a real logout failure. Confirmed via a real
browser: clicking Back after logout now re-triggers a fresh Keycloak login
instead of showing the cached page.

## Session cookie flags

The session cookie (an opaque ID -- see the server-side session note in
[gotchas.md](gotchas.md)) is `HttpOnly` by Flask's default (JS can't read
it) and `SameSite=Lax` (set explicitly in `app/product_app.py`) -- nothing
in this flow needs cross-site cookie sending, and Keycloak's redirects back
to this app are top-level navigations, which `Lax` always allows regardless
(confirmed via a real browser doing the full login/logout cycle after
enabling it).

`Secure` is **not** hardcoded on, and deliberately so: this whole stack runs
over plain HTTP locally (`KC_URL=http://localhost:8082`, this app on
`http://*.nip.io:3000`), and a browser silently refuses to ever send a
`Secure` cookie back over a non-HTTPS connection -- hardcoding it here would
break every login in this dev setup. It's instead gated on `KC_URL`'s
scheme (`app.config["SESSION_COOKIE_SECURE"] = KC_URL.startswith("https://")`),
reusing the project's existing browser-facing-URL signal rather than adding
a separate flag someone has to remember to flip for a real deployment.

## Manual verification

```bash
python3 scripts/setup.py   # provisions acme-idp, acme org, product-app client
python3 scripts/verify.py  # includes this feature's automated check
```

Then in a real browser: open `http://acme.sample.127.0.0.1.nip.io:3000/` —
should land directly on `acme-idp`'s login page (no email prompt). Log in
as `alice@acme.example.com` / `password123`. Land back on the product app
showing the logged-in email and `active_organization: acme`.
