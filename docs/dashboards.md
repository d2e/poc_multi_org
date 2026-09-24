# Dashboard

`scripts/provision_dashboards.py` (run automatically as the last step of
`setup.py`, and safe to re-run on its own) provisions an OpenSearch
Dashboards index pattern plus a dashboard at
`http://localhost:5601/app/dashboards#/view/keycloak-events-dashboard`.

## Panels

| Panel | Type | What it shows |
|---|---|---|
| **Event Volume Over Time** | Histogram | Event count over time, broken down by event type — the activity *pattern* |
| **Events by Type** | Pie chart | Breakdown of all events by `type.keyword` |
| **User Activity** | Table | Events per `authDetails.username`, most active first, sub-bucketed by event type — the *user journey* |
| **Org Activity** | Table | Events per `org_name` (excludes events with no org context) |
| **Tenant Activity** | Table | Events per `tenant`, sub-bucketed by event type — what a whole tenant (across all its env-orgs) has been doing, see [multi-env-tenants.md](multi-env-tenants.md) |
| **Login Issues** | Saved search (raw table) | Every `*ERROR*`/`*FAILURE*` event type, newest first — the *issues* |
| **Errors by Org** | Table | `*ERROR*`/`*FAILURE*` events grouped by `org_name`, sub-bucketed by type — *which* org's problems, not just that something errored |

## Designed for real-user-count scale, not just this POC's handful of events

At 100k real users, raw event counts stop being useful on their own —
routine successful logins and admin provisioning activity would otherwise
dominate every panel and bury the handful of events that actually matter.
Two things address this directly, both scoped deliberately rather than
applied blanket-wide:

- **`NOISE_FILTER = "not type: admin.*"`** is baked into the *general*
  activity panels only — Event Volume Over Time, Events by Type, User
  Activity — since `admin.*` events scale with how much provisioning/config
  work happens, not with real user activity, and would otherwise swamp
  those three specifically. It is **not** applied to Org Activity, Tenant
  Activity, Login Issues, or Errors by Org: an admin action can fail too,
  and org-lifecycle events (`admin.ORGANIZATION-CREATE` etc.) are
  meaningful signal there, not noise. This is set on each of those 3
  panels' own saved search query — deliberately *not* as a dashboard-level
  default query, since that combines (ANDs) with every panel including the
  ones that need to see everything, which would have silently suppressed
  admin-sourced errors dashboard-wide.
- **Errors by Org** exists because "N errors" is meaningless on its own at
  scale — what matters is *which* org has a disproportionate share of them.
  This panel's own query is scoped to error/failure types only, so it never
  has to compete with routine-activity volume in the first place, then
  groups by org so you know where to actually look. It doesn't compute an
  error *rate* (errors ÷ total events per org) — that needs either a
  scripted/pipeline metric or OpenSearch's Anomaly Detection plugin, both a
  bigger lift than this POC's scope; flagged here rather than silently
  left out.

## How it's built

Uses the OpenSearch Dashboards saved objects API (`/api/saved_objects/...`)
directly, not the UI wizard — same effect, but versioned and reproducible.
Every object (index pattern, 6 visualizations, 1 saved search, 1 dashboard)
uses a **fixed ID** with `?overwrite=true`, so re-running
`provision_dashboards.py` — or all of `setup.py` — after a
`docker compose down -v` recreates the same dashboard instead of
duplicating it.

Fixed IDs used:
- `keycloak-events-pattern` (index pattern, title `keycloak-events*`, time field `timestamp`)
- `viz-events-over-time`, `viz-events-by-type`, `viz-user-activity`, `viz-org-activity`, `viz-tenant-activity`, `viz-errors-by-org` (visualizations)
- `search-login-issues` (saved search)
- `keycloak-events-dashboard` (dashboard)

## Browser-only alternative: Dev Tools console

If you want to run ad-hoc OpenSearch queries from the browser instead of
`curl`, OpenSearch Dashboards has a built-in query console:

Left menu (☰) → **Management → Dev Tools**. Type a query and click the ▶
(play) button next to it, e.g.:
```
GET keycloak-events/_search
{
  "size": 0,
  "aggs": {
    "by_type": { "terms": { "field": "type.keyword", "size": 30 } }
  }
}
```
This talks to the same OpenSearch REST API as `curl` — just from the browser.

## Troubleshooting: "index pattern empty in Discover"

If Discover shows no results even though `curl .../keycloak-events/_count`
returns a nonzero count, the index pattern itself may not actually be saved
(check via `curl http://localhost:5601/api/saved_objects/_find?type=index-pattern`
— `total: 0` means nothing was saved, regardless of what the creation wizard
appeared to do). `provision_dashboards.py` creates it directly via the API to
avoid this; if you're troubleshooting an ad-hoc one you made by hand, just
re-run `python3 scripts/provision_dashboards.py`.

Also check the Discover time-range picker (top-right) — it defaults to a
short window (e.g. "Last 15 minutes") and can hide recent-but-not-recent-enough
data.

## Troubleshooting: "dashboard is not showing anything"

All panels blank with no visible error on the page itself — confirmed via a
real headless-browser load (`playwright`), not just guesswork, that this was
two separate hand-crafted-JSON schema bugs, both now fixed in
`provision_dashboards.py`:

1. **`searchSourceJSON.index` must not be a literal index-pattern ID.**
   OSD/Kibana resolves it indirectly: the field is `indexRefName` (a string
   naming a reference), and the actual ID lives in the saved object's own
   `references` array under a matching `name`. Inlining the literal ID under
   an `index` key (what seemed like the obvious approach) silently breaks
   every panel with zero error shown on the page.
2. **Every dashboard panel needs a `version` field.** Without it, OSD's
   panel migration code (`migratePanelsTo730` → `is640To720Panel`) throws
   `TypeError: Cannot read properties of null (reading 'version')` while
   loading the dashboard — visible only in the browser console, not on the
   page.

If you hit blank panels again after hand-editing this script, check the
browser console first (`F12` → Console) — the page itself won't show an
error, but OSD's own JS will.

## Troubleshooting: "Could not locate that index-pattern-field"

One specific panel errors with this message while the others render fine.
This happens when a field genuinely didn't exist in the index yet at the
moment the index pattern's field list was fetched/cached.

`fetch_fields()` in `provision_dashboards.py` handles the two ways this
actually occurs:
1. A **new field added later** in the pipeline's lifetime (e.g. if you add
   another enrichment field down the road) — re-run
   `python3 scripts/provision_dashboards.py` after that field has appeared
   in at least one real document to pick it up.
2. A **fresh `setup.py` run's own timing**: `provision_dashboards.py` runs
   as `setup.py`'s *last* step, but Vector's webhook→OpenSearch indexing
   still has a small lag behind the org-scoped events fired moments earlier
   by `provision_globex_tenant.py` — confirmed via a real dashboard load: on
   one clean run, `org_name`/`tenant` were both genuinely missing from the
   index pattern's field list despite the underlying events already
   existing, breaking the Org Activity and Tenant Activity panels with zero
   error on the page itself, silent until you actually looked. `fetch_fields()`
   now retries for up to 15s (checking every 2s) for the specific fields the
   dashboard's panels actually query by name (`org_name`, `tenant`, and
   their `.keyword` variants) before giving up and provisioning with
   whatever's available — self-healing on a normal run, and if it does time
   out, still logs exactly what's missing so you know to re-run once those
   events exist.

The same endpoint can also **404 outright** rather than just miss a field,
on a genuinely fresh `setup.py` run: if `keycloak-events` doesn't exist yet
in OpenSearch (Vector hasn't indexed anything yet at the exact moment
`provision_dashboards.py` runs, which is the last provisioning step), the
wildcard lookup 404s since there's nothing to match. `fetch_fields()`
tolerates this (provisions with an empty field list, logs a note) rather
than crashing setup.py — but the field list will then be empty until you
re-run `provision_dashboards.py` once real events exist.
