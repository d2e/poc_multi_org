"""Provisions an OpenSearch Dashboards index pattern + visualizations + a
dashboard over the keycloak-events index: event volume over time (pattern),
a per-actor activity table (user journey), a per-tenant activity table (see
docs/multi-env-tenants.md), a table isolating error/failure event types
(issues), and a raw switch-activity log (every active-organization switch,
who/from/to). Idempotent -- uses fixed saved-object IDs with
overwrite=true, so re-running this after a `docker compose down -v`
recreates the same dashboard rather than duplicating it.
"""
from __future__ import annotations

import os
import time

import requests

DASHBOARDS_URL = os.environ.get("OPENSEARCH_DASHBOARDS_URL", "http://localhost:5601")
INDEX_PATTERN_ID = "keycloak-events-pattern"
INDEX_PATTERN_TITLE = "keycloak-events*"

# Dashboards' API now requires auth (see docs/opensearch-auth.md) -- this
# script only ever does admin-level provisioning calls, so it authenticates
# as the OpenSearch bootstrap admin directly via Basic auth (Dashboards'
# opensearch_security.auth.type includes "basicauth" specifically so this
# keeps working with a one-line change, no OIDC flow needed for a script).
# Default matches docker-compose.yml's OPENSEARCH_INITIAL_ADMIN_PASSWORD --
# dev-only, not for production use.
AUTH = ("admin", os.environ.get("OPENSEARCH_ADMIN_PASSWORD", "Poc-Multi-Org-Dev-2026!"))

# admin.* events scale with how much provisioning/config work happens, not
# with real user activity -- at real user-count scale they'd swamp the
# general activity panels (Event Volume, Events by Type, User Activity).
# Deliberately NOT applied to the org/tenant activity panels (admin events
# carry meaningful org-lifecycle signal there) or the error-focused panels
# (an admin action can fail too; noise-filtering shouldn't hide that).
NOISE_FILTER = "not type: admin.*"

HEADERS = {"osd-xsrf": "true", "Content-Type": "application/json"}


def put_object(obj_type: str, obj_id: str, attributes: dict, references: list[dict] | None = None) -> None:
    resp = requests.post(
        f"{DASHBOARDS_URL}/api/saved_objects/{obj_type}/{obj_id}",
        params={"overwrite": "true"},
        headers=HEADERS,
        auth=AUTH,
        json={"attributes": attributes, "references": references or []},
        timeout=10,
    )
    resp.raise_for_status()
    print(f"saved {obj_type}/{obj_id}")


def search_source(query: str = "") -> str:
    import json

    # The index-pattern id must NOT be inlined as a literal "index" value --
    # OSD/Kibana resolves it indirectly via "indexRefName" against the
    # saved object's own "references" array (see index_pattern_ref() below).
    # Using a literal "index" here silently breaks every panel (blank, no
    # error) since the search source can't resolve which index pattern to
    # query against.
    return json.dumps(
        {
            "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
            "query": {"query": query, "language": "kuery"},
            "filter": [],
        }
    )


def index_pattern_ref() -> list[dict]:
    return [{"name": "kibanaSavedObjectMeta.searchSourceJSON.index", "type": "index-pattern", "id": INDEX_PATTERN_ID}]


def wait_for_dashboards(timeout_s: int = 120) -> None:
    print(f"waiting for OpenSearch Dashboards at {DASHBOARDS_URL} ...")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if requests.get(f"{DASHBOARDS_URL}/api/status", auth=AUTH, timeout=5).ok:
                print("OpenSearch Dashboards is up")
                return
        except requests.RequestException:
            pass
        time.sleep(3)
    raise SystemExit("OpenSearch Dashboards did not become ready in time")


def fetch_fields(pattern: str) -> str:
    """Fields discovered via field-caps, as the JSON string an index-pattern
    saved object expects in its own "fields" attribute. Populating this
    explicitly (rather than leaving it for OSD to lazily discover) matters
    here: a field that doesn't exist in the index yet at index-pattern
    creation time (e.g. org_name, only ever set on org-scoped events) won't
    be picked up later without this being re-run, and aggregations against
    a field missing from this cached list fail with "Could not locate that
    index-pattern-field" even though the field is genuinely in the mapping
    (confirmed via a real dashboard load, not just a schema guess -- this
    recurred on a fresh setup.py run despite the events existing, because
    `provision_dashboards.py` runs as setup.py's *last* step and still beat
    Vector's webhook->OpenSearch indexing lag for the org-scoped events fired
    moments earlier by provision_globex_tenant.py).

    Retries for a few seconds if the panels' required fields aren't there
    yet, rather than either failing setup.py or silently shipping a
    dashboard with permanently-broken panels until someone notices and
    re-runs this script by hand.
    """
    import json

    # Every field a panel in this dashboard actually queries by name (not
    # just displays) -- if these are missing, that panel is broken.
    required = {"org_name", "org_name.keyword", "tenant", "tenant.keyword"}
    deadline = time.monotonic() + 15
    fields: list[dict] = []
    while True:
        resp = requests.get(
            f"{DASHBOARDS_URL}/api/index_patterns/_fields_for_wildcard",
            params={"pattern": pattern, "meta_fields": '["_source","_id","_type","_index","_score"]'},
            auth=AUTH,
            timeout=10,
        )
        if resp.status_code == 404:
            # On a genuinely fresh setup.py run, Vector may not have indexed
            # the first event yet (the underlying index doesn't exist).
            fields = []
        else:
            resp.raise_for_status()
            fields = resp.json()["fields"]

        if required <= {f["name"] for f in fields} or time.monotonic() > deadline:
            break
        print(f"waiting for {sorted(required - {f['name'] for f in fields})} to appear in {pattern} ...")
        time.sleep(2)

    missing = required - {f["name"] for f in fields}
    if missing:
        print(
            f"proceeding without {sorted(missing)} -- re-run this script "
            "once those events exist (see docs/dashboards.md)"
        )
    return json.dumps(fields)


def main() -> None:
    import json

    wait_for_dashboards()
    put_object(
        "index-pattern",
        INDEX_PATTERN_ID,
        {
            "title": INDEX_PATTERN_TITLE,
            "timeFieldName": "timestamp",
            "fields": fetch_fields(INDEX_PATTERN_TITLE),
        },
    )

    # 1. Pattern: event volume over time
    put_object(
        "visualization",
        "viz-events-over-time",
        {
            "title": "Event Volume Over Time",
            "visState": json.dumps(
                {
                    "title": "Event Volume Over Time",
                    "type": "histogram",
                    "params": {"grid": {"categoryLines": False}, "legendPosition": "right"},
                    "aggs": [
                        {"id": "1", "enabled": True, "type": "count", "schema": "metric", "params": {}},
                        {
                            "id": "2",
                            "enabled": True,
                            "type": "date_histogram",
                            "schema": "segment",
                            "params": {"field": "timestamp", "interval": "auto"},
                        },
                        {
                            "id": "3",
                            "enabled": True,
                            "type": "terms",
                            "schema": "group",
                            "params": {"field": "type.keyword", "size": 8, "order": "desc", "orderBy": "1"},
                        },
                    ],
                }
            ),
            "uiStateJSON": "{}",
            "description": "",
            "kibanaSavedObjectMeta": {"searchSourceJSON": search_source(NOISE_FILTER)},
        },
        index_pattern_ref(),
    )

    # 2. Pattern: breakdown by event type
    put_object(
        "visualization",
        "viz-events-by-type",
        {
            "title": "Events by Type",
            "visState": json.dumps(
                {
                    "title": "Events by Type",
                    "type": "pie",
                    "params": {"isDonut": True},
                    "aggs": [
                        {"id": "1", "enabled": True, "type": "count", "schema": "metric", "params": {}},
                        {
                            "id": "2",
                            "enabled": True,
                            "type": "terms",
                            "schema": "segment",
                            "params": {"field": "type.keyword", "size": 20, "order": "desc", "orderBy": "1"},
                        },
                    ],
                }
            ),
            "uiStateJSON": "{}",
            "description": "",
            "kibanaSavedObjectMeta": {"searchSourceJSON": search_source(NOISE_FILTER)},
        },
        index_pattern_ref(),
    )

    # 3. User journey: per-actor activity, most active first, with their most
    # recent event type as a sub-bucket so you can see what each user has been
    # doing (login, org membership change, org switch, ...).
    put_object(
        "visualization",
        "viz-user-activity",
        {
            "title": "User Activity",
            "visState": json.dumps(
                {
                    "title": "User Activity",
                    "type": "table",
                    "params": {"perPage": 20, "showPartialRows": False, "showMetricsAtAllLevels": False},
                    "aggs": [
                        {"id": "1", "enabled": True, "type": "count", "schema": "metric", "params": {}},
                        {
                            "id": "2",
                            "enabled": True,
                            "type": "terms",
                            "schema": "bucket",
                            "params": {
                                "field": "authDetails.username.keyword",
                                "size": 25,
                                "order": "desc",
                                "orderBy": "1",
                                "missingBucket": False,
                            },
                        },
                        {
                            "id": "3",
                            "enabled": True,
                            "type": "terms",
                            "schema": "bucket",
                            "params": {
                                "field": "type.keyword",
                                "size": 5,
                                "order": "desc",
                                "orderBy": "1",
                            },
                        },
                    ],
                }
            ),
            "uiStateJSON": "{}",
            "description": "Which users did what, most active first",
            "kibanaSavedObjectMeta": {"searchSourceJSON": search_source(NOISE_FILTER)},
        },
        index_pattern_ref(),
    )

    # 4. Org activity: which orgs events are actually tied to (membership
    # changes, active-org switches) -- most events have no org context, so
    # this excludes docs missing org_name rather than showing a giant
    # "missing" bucket.
    put_object(
        "visualization",
        "viz-org-activity",
        {
            "title": "Org Activity",
            "visState": json.dumps(
                {
                    "title": "Org Activity",
                    "type": "table",
                    "params": {"perPage": 10, "showPartialRows": False, "showMetricsAtAllLevels": False},
                    "aggs": [
                        {"id": "1", "enabled": True, "type": "count", "schema": "metric", "params": {}},
                        {
                            "id": "2",
                            "enabled": True,
                            "type": "terms",
                            "schema": "bucket",
                            "params": {
                                "field": "org_name.keyword",
                                "size": 10,
                                "order": "desc",
                                "orderBy": "1",
                                "missingBucket": False,
                            },
                        },
                    ],
                }
            ),
            "uiStateJSON": "{}",
            "description": "",
            "kibanaSavedObjectMeta": {"searchSourceJSON": search_source()},
        },
        index_pattern_ref(),
    )

    # 5. Tenant activity: events grouped by tenant (see docs/multi-env-tenants.md)
    # then by event type -- lets you answer "what has tenant X been doing"
    # across all its env-orgs (globex-dev/stage/prod) in one place, rather
    # than per-org like viz-org-activity above. Only tenant-tagged orgs
    # (globex-*) show up here; org1/2/3/acme are untagged standalone tenants.
    put_object(
        "visualization",
        "viz-tenant-activity",
        {
            "title": "Tenant Activity",
            "visState": json.dumps(
                {
                    "title": "Tenant Activity",
                    "type": "table",
                    "params": {"perPage": 10, "showPartialRows": False, "showMetricsAtAllLevels": False},
                    "aggs": [
                        {"id": "1", "enabled": True, "type": "count", "schema": "metric", "params": {}},
                        {
                            "id": "2",
                            "enabled": True,
                            "type": "terms",
                            "schema": "bucket",
                            "params": {
                                "field": "tenant.keyword",
                                "size": 10,
                                "order": "desc",
                                "orderBy": "1",
                                "missingBucket": False,
                            },
                        },
                        {
                            "id": "3",
                            "enabled": True,
                            "type": "terms",
                            "schema": "bucket",
                            "params": {"field": "type.keyword", "size": 5, "order": "desc", "orderBy": "1"},
                        },
                    ],
                }
            ),
            "uiStateJSON": "{}",
            "description": "Which tenant (across all its env-orgs) events belong to",
            "kibanaSavedObjectMeta": {"searchSourceJSON": search_source()},
        },
        index_pattern_ref(),
    )

    # 6. Issues: raw table of every error/failure-shaped event type, newest
    # first -- a saved search rather than an aggregation, since the point is
    # to read each incident, not count them.
    put_object(
        "search",
        "search-login-issues",
        {
            "title": "Login Issues",
            "description": "Every *ERROR*/*FAILURE* event, newest first",
            "columns": ["timestamp", "type", "authDetails.username", "authDetails.ipAddress", "details"],
            "sort": [["timestamp", "desc"]],
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": search_source("type.keyword: *ERROR* or type.keyword: *FAILURE*")
            },
        },
        index_pattern_ref(),
    )

    # 7. Errors by org: at real user-count scale, "N errors" is meaningless
    # noise on its own -- what matters is *which org* is having a
    # disproportionate share of them. This panel's own query is scoped to
    # error/failure event types only (same as Login Issues), so it never
    # competes with routine-activity volume; grouping by org_name then type
    # tells you where to actually look, not just that something errored.
    put_object(
        "visualization",
        "viz-errors-by-org",
        {
            "title": "Errors by Org",
            "visState": json.dumps(
                {
                    "title": "Errors by Org",
                    "type": "table",
                    "params": {"perPage": 10, "showPartialRows": False, "showMetricsAtAllLevels": False},
                    "aggs": [
                        {"id": "1", "enabled": True, "type": "count", "schema": "metric", "params": {}},
                        {
                            "id": "2",
                            "enabled": True,
                            "type": "terms",
                            "schema": "bucket",
                            "params": {
                                "field": "org_name.keyword",
                                "size": 10,
                                "order": "desc",
                                "orderBy": "1",
                                "missingBucket": True,
                                "missingBucketLabel": "(no org context)",
                            },
                        },
                        {
                            "id": "3",
                            "enabled": True,
                            "type": "terms",
                            "schema": "bucket",
                            "params": {"field": "type.keyword", "size": 5, "order": "desc", "orderBy": "1"},
                        },
                    ],
                }
            ),
            "uiStateJSON": "{}",
            "description": "Error/failure events grouped by org, not raw volume -- where to actually look at scale",
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": search_source("type.keyword: *ERROR* or type.keyword: *FAILURE*")
            },
        },
        index_pattern_ref(),
    )

    # 8. Switch activity: every active-organization switch, newest first --
    # a saved search (not an aggregation) since the point is to read each
    # switch (who, from which env-org to which, for which tenant), not just
    # count them the way viz-tenant-activity/viz-user-activity already do.
    # Keycloak's switch-organization endpoint doesn't emit its own event type
    # -- it logs as a plain access.UPDATE_PROFILE, so this filters further on
    # details.new_active_organization_id existing to exclude any other
    # profile-update event that isn't actually a switch (confirmed via a
    # live query against real switch events driven by verify.py, not assumed
    # from the event type name alone).
    put_object(
        "search",
        "search-switch-activity",
        {
            "title": "Switch Activity",
            "description": "Every active-organization switch, newest first",
            "columns": [
                "timestamp",
                "authDetails.username",
                "tenant",
                "details.previous_active_organization_name",
                "details.new_active_organization_name",
            ],
            "sort": [["timestamp", "desc"]],
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": search_source(
                    'type.keyword: "access.UPDATE_PROFILE" and details.new_active_organization_id: *'
                )
            },
        },
        index_pattern_ref(),
    )

    # Dashboard combining all of the above.
    # Each panel needs "version" -- without it, OSD's dashboard panel
    # migration (migratePanelsTo730 -> is640To720Panel) crashes with
    # "Cannot read properties of null (reading 'version')" while loading the
    # dashboard, rendering every panel blank with no visible error on the
    # page itself (only in the browser console). Confirmed via a real
    # headless-browser load, not just a schema guess.
    panel_version = "7.3.0"
    panels = [
        {"version": panel_version, "gridData": {"x": 0, "y": 0, "w": 48, "h": 15, "i": "1"}, "panelIndex": "1", "embeddableConfig": {}, "panelRefName": "panel_1"},
        {"version": panel_version, "gridData": {"x": 0, "y": 15, "w": 24, "h": 15, "i": "2"}, "panelIndex": "2", "embeddableConfig": {}, "panelRefName": "panel_2"},
        {"version": panel_version, "gridData": {"x": 24, "y": 15, "w": 24, "h": 15, "i": "3"}, "panelIndex": "3", "embeddableConfig": {}, "panelRefName": "panel_3"},
        {"version": panel_version, "gridData": {"x": 0, "y": 30, "w": 24, "h": 15, "i": "4"}, "panelIndex": "4", "embeddableConfig": {}, "panelRefName": "panel_4"},
        {"version": panel_version, "gridData": {"x": 24, "y": 30, "w": 24, "h": 15, "i": "5"}, "panelIndex": "5", "embeddableConfig": {}, "panelRefName": "panel_5"},
        {"version": panel_version, "gridData": {"x": 0, "y": 45, "w": 24, "h": 15, "i": "6"}, "panelIndex": "6", "embeddableConfig": {}, "panelRefName": "panel_6"},
        {"version": panel_version, "gridData": {"x": 24, "y": 45, "w": 24, "h": 15, "i": "7"}, "panelIndex": "7", "embeddableConfig": {}, "panelRefName": "panel_7"},
        {"version": panel_version, "gridData": {"x": 0, "y": 60, "w": 48, "h": 15, "i": "8"}, "panelIndex": "8", "embeddableConfig": {}, "panelRefName": "panel_8"},
    ]
    put_object(
        "dashboard",
        "keycloak-events-dashboard",
        {
            "title": "Keycloak Events",
            "description": "Event pattern, per-user journey, and login issues over the platform realm",
            "panelsJSON": json.dumps(panels),
            "optionsJSON": json.dumps({"useMargins": True, "hidePanelTitles": False}),
            "version": 1,
            "timeRestore": True,
            "timeFrom": "now-24h",
            "timeTo": "now",
            # Deliberately no dashboard-level query here: the top query bar
            # combines (AND) with *every* panel's own query, which would
            # suppress admin-sourced errors on Login Issues/Errors by
            # Org/Org Activity/Tenant Activity too -- NOISE_FILTER is
            # already baked into just the 3 general-activity panels' own
            # saved searches above, which is the correct scope.
            "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps({"query": {"query": "", "language": "kuery"}, "filter": []})},
        },
        [
            {"name": "panel_1", "type": "visualization", "id": "viz-events-over-time"},
            {"name": "panel_2", "type": "visualization", "id": "viz-events-by-type"},
            {"name": "panel_3", "type": "visualization", "id": "viz-user-activity"},
            {"name": "panel_4", "type": "visualization", "id": "viz-org-activity"},
            {"name": "panel_5", "type": "search", "id": "search-login-issues"},
            {"name": "panel_6", "type": "visualization", "id": "viz-tenant-activity"},
            {"name": "panel_7", "type": "visualization", "id": "viz-errors-by-org"},
            {"name": "panel_8", "type": "search", "id": "search-switch-activity"},
        ],
    )
    print(f"\nDashboard ready: {DASHBOARDS_URL}/app/dashboards#/view/keycloak-events-dashboard")


if __name__ == "__main__":
    main()
