# Two federated IdPs on one org: which one wins?

## What this demonstrates

Every other org in this project (`org1`/`org2`/`org3`/`acme`/`globex-dev`) is
linked to exactly one federated IdP. phasetwo's `keycloak-orgs` extension
actually supports linking **more than one** IdP to a single org — this is a
standalone test scenario (`scripts/provision_dual_idp_scenario.py`) built to
answer, empirically, three questions: can an org actually have two IdPs at
once, how does Keycloak decide which one handles a login, and how do you
make that decision deterministic instead of implicit.

Everything below was confirmed by decompiling the actual
`io.phasetwo.keycloak-keycloak-orgs-0.173.jar` classes shipped in this
project's `phasetwo-keycloak` image (`javap -p -c`, not phasetwo's docs or a
blog post — same "read the real source" standard as
[multi-env-tenants.md](multi-env-tenants.md)'s org-role API and
[ciba-support-delegation.md](ciba-support-delegation.md)'s CIBA endpoints),
then verified end-to-end against the real running stack with
`scripts/test_dual_idp_precedence.py`.

## The scenario

`scripts/provision_dual_idp_scenario.py` creates:
- Two independent federated realms, `dualidp-alpha-idp` and
  `dualidp-beta-idp`, each with a broker client and a test user
  `alice@dualidp.example.com` (standing in for two different corporate IdPs
  that both claim the same company — a merger, or an in-progress IdP
  migration, are the realistic versions of this).
- One org, `dualidp`, with **three** domains: the shared
  `dualidp.example.com`, plus `alpha.dualidp.example.com` and
  `beta.dualidp.example.com` (see Scenario C below for why).
- Both IdPs linked to that one org.

Run it, then `scripts/test_dual_idp_precedence.py` to reproduce every finding
below against the live stack.

## Finding 1: by default, an org can only have ONE active IdP — silently

`POST /realms/{realm}/orgs/{orgId}/idps` (the same endpoint
`provision_platform_orgs.py` already uses for every other org) does not
error when you call it a second time for the same org with a different IdP.
It succeeds with `201`, **and silently un-links the previous IdP** from that
org: its `home.idp.discovery.org` config attribute (the actual field
Keycloak's discovery query matches against) is cleared.

Confirmed directly:
```
# after linking dualidp-alpha, then dualidp-beta, to the same org:
dualidp-alpha home.idp.discovery.org= None
dualidp-beta  home.idp.discovery.org= 350a4887-8eef-42a4-90bf-d2d9d73de0a1
```
`dualidp-alpha` still exists as a perfectly enabled realm IdP — it's just
silently orphaned from the org, with **no error, warning, or 409** to signal
it. Decompiling `IdentityProvidersResource.createIdentityProvider()`
confirms this is deliberate:
```java
deactivateOtherIdps(newIdpRep, !IdentityProviders.isMultipleIdpsConfigEnabled(realm), orgId);
```
This is exactly the realm-wide toggle phasetwo calls "enable multiple IdPs
for an org" — `multipleIdpsEnabled` in the org config
(`GET/PUT /realms/{realm}/orgs/config`, JSON field `multipleIdps`; **off** by
default). With it off, `deactivateOtherIdps` runs and unlinks whatever was
there before. **This must be turned on before linking a second IdP**, or the
first one silently stops working for that org — no exception is raised, so
nothing in a normal integration test would catch it.

In the admin console UI (phasetwo Admin Portal), this is the **"Multiple
IDPs enabled"** toggle (confirmed from the actual `phasetwo-admin-ui` theme
bundle: `multipleIdpsEnabled=Multiple IDPs enabled`, explainer text "With
this config enabled, users will be able to assign multiple Identity
Providers to an organization"). It lives on the **"Manage Organizations
Settings"** page — a realm-wide page, not a per-org one ("Configure the
settings for organizations. This will apply globally to all organizations"),
consistent with it being backed by `/realms/{realm}/orgs/config` rather than
any single org's own settings.

`provision_dual_idp_scenario.py` always enables `multipleIdpsEnabled`
before linking its second IdP, specifically to avoid this trap.

Confirmed **safe** for the rest of this project: `multipleIdpsEnabled` is
realm-wide, not per-org, so turning it on for `platform` in principle
affects every org — but it's a no-op for `org1`/`org2`/`org3`/`acme`/`globex-dev`
since each has exactly one linked IdP already (nothing to deactivate) and
none declares a `home.idp.discovery.domains` value (see Finding 3) for the
extra filtering it enables to ever activate. `scripts/verify.py`'s full
suite passes unchanged with it on.

## Finding 2: with two IdPs truly linked, precedence is implementation-defined

Once both IdPs are genuinely linked (`multipleIdpsEnabled=true`, both retain
`home.idp.discovery.org`), a login attempt for
`alice@dualidp.example.com` (the domain both share) reaches
`HomeIdpDiscoveryAuthenticator.redirectOrChallenge()` with a 2-element
candidate list. Decompiled logic:
```java
if (list.size() == 1 || config.forwardToFirstMatch()) {
    redirector.redirectTo(list.get(0));   // silent redirect, no user choice
} else {
    authenticationChallenge().forceChallenge(list);  // real IdP picker page
}
```
This project's `forwardToFirstMatch` is `true` (set in
`provision_browser_flow.py`, same authenticator config every other org's
login already goes through) — so with two real candidates, the login
**silently auto-redirects to just one**, with **no error and no picker**.
Confirmed live:
```
=== Scenario A: broad-domain login, forwardToFirstMatch=true ===
RESULT: auto-redirected to 'dualidp-alpha' with no picker shown
```
**Which one wins is not something this project (or, as far as this
investigation went, phasetwo's public docs) makes configurable or
documented as stable.** `list.get(0)`'s order comes from
`OrganizationProvider.getOrganizationsStreamForDomain()`'s own query order,
not any explicit priority/weight field on the org-IdP link. Treat this as
implementation-defined — don't build a real deployment that depends on which
of two ambiguous IdPs "wins".

Setting `forwardToFirstMatch=false` instead produces a real picker page
listing both IdPs, letting the user choose — confirmed live (Scenario B).
This is the only *documented, intentional* way to handle real ambiguity: ask
the user, don't guess.

## Finding 3 (the actually-recommended fix): narrow each IdP to its own domain

Each linked IdP can declare its own `home.idp.discovery.domains` config
value (comma-separated, or set via the `domains` field on the
`POST /orgs/{orgId}/idps/link` "link an existing IdP" endpoint). With
`multipleIdpsEnabled` on, `EmailHomeIdpDiscoverer.discoverHomeIdps()` filters
the org's candidate IdPs down to just the ones whose own declared domain
matches — and only falls back to the ambiguous full candidate list (Finding
2) if that filtered set comes up empty.

`provision_dual_idp_scenario.py` sets this up from the start: the org has
domains `dualidp.example.com` (shared/ambiguous), `alpha.dualidp.example.com`
(claimed only by `dualidp-alpha`), and `beta.dualidp.example.com` (claimed
only by `dualidp-beta`). Confirmed live, and — importantly — confirmed to
hold **regardless of `forwardToFirstMatch`**, since narrowing brings the
candidate list down to exactly one, which trips the authenticator's
`list.size() == 1` branch on its own:
```
=== Scenario C: per-IdP narrow-domain logins, forwardToFirstMatch=false ===
OK: alice@alpha.dualidp.example.com -> dualidp-alpha (expected dualidp-alpha)
OK: alice@beta.dualidp.example.com  -> dualidp-beta  (expected dualidp-beta)
```
This is the pattern to actually use for a real "two IdPs, one org" rollout:
give each IdP its own claimed sub-domain (or distinct verified email domain)
via `home.idp.discovery.domains`, rather than relying on
`forwardToFirstMatch`'s implicit, unconfigurable ordering. A login on the
genuinely shared/ambiguous domain still falls back to Finding 2's behavior —
which is correct, since at that point Keycloak really doesn't have grounds
to prefer one IdP over the other.

## Summary

| Setup | Login on shared domain | Login on IdP's own narrow domain |
|---|---|---|
| 1 IdP linked (every other org in this project) | routes to that IdP | n/a |
| 2 IdPs linked, `multipleIdpsEnabled=false` | 2nd link silently evicted the 1st (Finding 1) — routes to whichever is still linked | n/a |
| 2 IdPs linked, `multipleIdpsEnabled=true`, no per-IdP domain | `forwardToFirstMatch=true`: silent, implementation-defined pick (Finding 2). `forwardToFirstMatch=false`: real picker page | n/a |
| 2 IdPs linked, `multipleIdpsEnabled=true`, each with `home.idp.discovery.domains` | falls back to the row above | deterministic, exact match (Finding 3 — recommended) |

## Reproducing

```bash
python3 scripts/provision_dual_idp_scenario.py   # idempotent, safe to re-run
python3 scripts/test_dual_idp_precedence.py      # drives all 3 scenarios, restores config after
```
