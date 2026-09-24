# Multi-environment tenant: switching orgs within one tenant

## What this demonstrates

A different relationship than the rest of this POC's orgs: instead of "one
org = one standalone tenant" (`org1`/`org2`/`org3`/`acme`), tenant **`globex`**
owns **three** orgs, each representing a deployment environment of the same
product — `globex-dev`, `globex-stage`, `globex-prod`. A user who's a member
of more than one can switch their active org between them (reusing the
existing active-organization switch mechanism — no new Keycloak-side
mechanism needed for the switch itself), and can hold a **different role in
each environment**.

```
alice@globex.example.com logs in once via globex-idp (corporate SSO)
  -> lands in globex-dev, role: admin
  -> switches to globex-stage, role: developer
  -> switches to globex-prod, role: viewer
```

## The tenant/env/role model

- **Tenant grouping is a real Keycloak org attribute**, not just a naming
  convention: each of the 3 envs has `attributes: {"tenant": ["globex"]}`.
  This is cheap — the existing `active-organization` client scope's mapper
  already includes `attribute` in `included.active.organization.properties`
  (see `provision_active_org_client.py`), so `active_organization.attribute.tenant`
  shows up in the ID token for free, no new mapper config needed.
- **Org-scoped roles are a real `keycloak-orgs` capability**, confirmed via
  the actual extension source
  (`RoleResource.java`/`RolesResource.java` in
  [p2-inc/keycloak-orgs](https://github.com/p2-inc/keycloak-orgs)) — this
  repo had never used it before this feature; the `active_organization.role`
  claim had always been empty. The REST shape:
  - `POST /realms/{realm}/orgs/{orgId}/roles` `{name, description}` — define a role (scoped to that one org, even if same-named as a role in another org)
  - `PUT /realms/{realm}/orgs/{orgId}/roles/{roleName}/users/{userId}` — grant it
  - `GET`/`DELETE` variants for listing/revoking
- **Only `globex-dev` has a broker/IdP link.** `globex-stage` and
  `globex-prod` are pure membership/role containers on the `platform` realm
  — there's one corporate IdP for the whole tenant (one login), and
  environment separation happens entirely on the platform side via org
  membership, not via separate logins per environment.

## A real sequencing problem, and how it's solved

`alice@globex.example.com` is federated, not local — her `platform`-realm
account only gets created on her **first broker login** (sync mode IMPORT),
same as `org1`/`org2`/`org3`/`acme`'s users. But granting her
`globex-stage`/`globex-prod` membership + roles during `setup.py` needs her
`platform`-realm user ID to already exist — a chicken-and-egg problem
`provision_demo_user.py` never had (`shared.user` is a *local* user, created
directly, no login needed first).

`provision_globex_tenant.py` solves this by **driving one real login
itself**, reusing the exact browser-simulation machinery `verify.py` already
had — extracted into a shared `scripts/browser_login.py` module
(`PlainCookieJarSession`, `form_action`, `tenant_get`) so neither file
duplicates it. That bootstrap login:
1. Creates alice's `platform`-realm account (via IMPORT sync).
2. Adds her to `globex-dev` automatically (via the existing `post org broker
   login` flow — the same auto-join mechanism org1/2/3/acme already use).

After that, the script looks up her new user ID directly and grants the
remaining membership + all three roles via plain admin API calls.

## Scope limitation: tenant isolation is app-level only

Keycloak's `switch-organization` endpoint has **no concept of "tenant"** —
it only checks membership. Nothing stops a user from switching to *any* org
they're a member of via the raw API, tenant or not. This project's
`app/product_app.py` only ever *offers* same-tenant switch targets
(`MULTI_ENV_TENANTS`), and its `/switch/<org_name>` route double-checks the
target is in that list before calling Keycloak — but that's a UI/app-level
restriction, not a server-side guarantee. True tenant isolation at the
Keycloak level would need a custom Keycloak extension (e.g. a custom
`switch-organization` policy), which is out of scope here. `verify.py`'s
cross-tenant test confirms the app-level check works, not that Keycloak
itself would reject it.

## Role enforcement: `require_role()` and `/admin-only`

Until this was added, `active_organization.role` was decoded and *displayed*
on the index page but never actually **checked** anywhere — every route
behaved identically regardless of whether the caller's role was `admin` or
`viewer`. `require_role(role)` in `app/product_app.py` is the enforcement
point: a route decorator that 403s unless `role` is in the *current* active
org's role list. `/admin-only` is a demo route using it, and the index page
links to it directly so you can see it fail after switching to a non-admin
env.

The check is deliberately re-evaluated on every request against whatever
org is active **right now**, not a role captured at login time — since a
user's role is scoped per-org (`alice` is `admin` in `globex-dev` and
`viewer` in `globex-prod` simultaneously, see above), caching the role at
login would silently grant stale admin access after a switch to a
lower-privileged env. `scripts/verify.py` regression-tests both directions:
`200` right after switching to `globex-dev` (admin), `403` right after
switching to `globex-prod` (viewer), same session, same route.

This is a demo enforcement point, not a complete authorization system —
there's still no resource/API layer beyond this one route, and no
role-hierarchy concept (`admin` implies `developer` implies `viewer`, or
similar) if that's ever needed. It exists to close a real gap: a token
carrying a role claim that nothing ever consults is equivalent to not
having roles at all.

## Finding tenant activity in the event log

The `tenant` custom attribute isn't just for the token claim — it's also
surfaced on every OpenSearch event (login, membership, switch) via a
top-level `tenant` field, and broken out in the **Tenant Activity** panel on
the [provisioned dashboard](dashboards.md). See
[event-logging.md](event-logging.md)'s "Tenant enrichment" section for how
this resolves even for login events, which carry no org id at all.

## Gotcha: active org is a persisted user attribute, not "always defaults to the first org"

Keycloak's active-organization feature (see
[keycloak-orgs' docs](https://github.com/p2-inc/keycloak-orgs/blob/main/docs/active-organization.md))
stores the active org in a user attribute (`org.ro.active`) that **persists
across logins**. "Defaults to the first org a user belongs to" only applies
the very first time, before that attribute is ever set. This tripped up
`verify.py`'s own test during development: after manually testing the
switch flow by hand, a *fresh* automated test run found alice's active org
already sitting on whatever was last switched to (`globex-prod`), not
`globex-dev` — correct Keycloak behavior, but it means a test (or any code)
can't assume "first login always lands on org X." `verify.py` handles this
by explicitly switching to `globex-dev` right after login, rather than
asserting on whatever a fresh login happened to restore.

## Verification

```bash
python3 scripts/setup.py   # provisions globex-idp, the 3 envs, roles, alice's memberships
python3 scripts/verify.py  # includes this feature's automated test (login, 2 switches, cross-tenant rejection, role enforcement)
```

Manual: open `http://globex.sample.127.0.0.1.nip.io:3000/`, log in as
`alice@globex.example.com` / `password123` — lands in `globex-dev` as
`admin`. Use the switch links to move to `globex-stage` (`developer`) and
`globex-prod` (`viewer`). Confirm the existing `acme` and `org1`/`org2`/`org3`
demos are unaffected. Click the **Admin-only demo page** link while on
`globex-dev` (works), then switch to `globex-stage` or `globex-prod` and
click it again (403s) — see "Role enforcement" above.
