# Testing

## Automated: `scripts/verify.py`

```bash
python3 scripts/verify.py
```

Three checks:
1. **Federated login** — drives a real browser-style login for
   `alice@org3.example.com` through Home IdP Discovery → `org3-idp` → the
   broker callback chain, confirms the final token carries
   `active_organization.name == "org3"`.
2. **Active-organization switch** — logs in `shared.user` (member of org1 +
   org2), confirms the default active org, calls `switch-organization`,
   confirms the claim updates to org2.
3. **Subdomain-based IdP routing** (see
   [tenant-routing.md](tenant-routing.md)) — hits the `product-app`
   container's `acme` tenant, confirms `kc_idp_hint=acme` routes straight
   to `acme-idp`'s login page (never Home IdP Discovery's email prompt),
   logs in, confirms the app shows `active_organization: acme` after the
   callback, and confirms logout ends the Keycloak SSO session (not just
   the app's own cookie). Connects directly to `127.0.0.1:3000` with a
   `Host` header override rather than relying on nip.io's DNS actually
   resolving — more portable, works offline too.
4. **Multi-env tenant switching** (see
   [multi-env-tenants.md](multi-env-tenants.md)) — logs in as
   `alice@globex.example.com`, forces the active org to `globex-dev` (role
   `admin`), switches to `globex-stage` (role `developer`) then
   `globex-prod` (role `viewer`), and confirms an attempted switch to
   `org1` (a different tenant, not a member) is rejected by the app rather
   than silently allowed.

This drives real HTML forms and redirect chains, not just JSON APIs, which
surfaced two non-obvious `requests` limitations along the way — see
[gotchas.md](gotchas.md) for `PlainCookieJarSession`, the hand-rolled
path-scoped cookie jar this needed (now shared via `scripts/browser_login.py`,
also used by `provision_globex_tenant.py` to bootstrap a federated user's
account — see [multi-env-tenants.md](multi-env-tenants.md)).

## Manual: from a browser

**1. Federated org login.** Open
`http://localhost:8082/realms/platform/account/` (use a private/incognito
window so you don't collide with an existing session). Enter
`alice@org1.example.com`, continue — Home IdP Discovery redirects you to the
`org1-idp` realm's own login page. Log in there with `password123`. You land
back in `platform`'s account console, now a member of `org1`. Repeat for
`alice@org2.example.com` / `alice@org3.example.com` to see the domain-routing
work for each org.

**2. Local password login.** Same URL, log in with `shared.user` /
`password123` — skips the IdP redirect since it's a local account already in
`platform`.

**3. Active-organization switch.** No UI button for this — it's a raw API
demo. Easiest from a terminal:
```bash
# password grant just for demo purposes; a real app should use the browser/PKCE flow
TOK=$(curl -s -X POST http://localhost:8082/realms/platform/protocol/openid-connect/token \
  -d client_id=poc-app -d username=shared.user -d password=password123 -d grant_type=password \
  | jq -r .access_token)

ORG2_ID=$(cat scripts/.state/org2.id)
curl -s -X PUT http://localhost:8082/realms/platform/users/switch-organization \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d "{\"id\":\"$ORG2_ID\"}"
# -> returns a new token set; decode it at jwt.io, .active_organization is now org2
```

**4. Watch it land in OpenSearch.** See [dashboards.md](dashboards.md) and
[event-logging.md](event-logging.md) for Discover / Dev Tools / the
provisioned dashboard.

**5. Admin's-eye view.** `http://localhost:8082` → `admin`/`admin` → switch
to the `platform` realm (top-left dropdown) → **Organizations** to see org
membership change live as you log in with different `alice@orgN` users, or
**Realm settings → Events** to see the webhook config.

**6. Subdomain-based IdP routing.** Open
`http://acme.sample.127.0.0.1.nip.io:3000/` in a browser (private/incognito
window). You should land **directly** on `acme-idp`'s login page — no email
prompt, no IdP picker. Log in with `alice@acme.example.com` / `password123`.
You land back on the product app showing your email and
`active_organization: acme`. See [tenant-routing.md](tenant-routing.md) if
this URL doesn't resolve (nip.io DNS issue) for the `/etc/hosts` fallback.

**7. Multi-env tenant switching.** Open
`http://globex.sample.127.0.0.1.nip.io:3000/` in a browser. Log in with
`alice@globex.example.com` / `password123` — lands in `globex-dev` as
`admin`. Use the "Switch environment" links on the page to move to
`globex-stage` (`developer`) and `globex-prod` (`viewer`). See
[multi-env-tenants.md](multi-env-tenants.md).

## Troubleshooting: "Invalid username or password" on an org-idp login page

If this happens on a *second or later* attempt against a realm you've
rebuilt (`docker compose down -v` + re-provision) since the browser tab was
first opened, it's very likely a **stale tab**: the login page/session
references a realm state that no longer exists server-side, so even the
correct password fails legitimately. Open a brand new tab and start the
flow again from `http://localhost:8082/realms/platform/account/`.

To rule out a genuine credential problem, check directly:
```bash
python3 -c "
import sys; sys.path.insert(0,'scripts')
from keycloak_admin import AdminClient, admin_token
admin = AdminClient(token=admin_token())
users = admin.get('/admin/realms/org1-idp/users?username=alice@org1.example.com').json()
print(users)
uid = users[0]['id']
print(admin.get(f'/admin/realms/org1-idp/attack-detection/brute-force/users/{uid}').json())
"
```
This checks the user exists, is enabled, and isn't locked out by Keycloak's
brute-force protection.
