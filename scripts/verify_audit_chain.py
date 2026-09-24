"""Verifies the tamper-evident hash chain audit_chain_signer.py attaches to
every keycloak-events document (see docs/audit-log-integrity.md). Run this
any time you want to check whether the audit log has been tampered with --
it's a standalone check, not part of setup.py.

Detects four distinct kinds of tampering, each with a different signature:
  - The visible/queryable fields altered without touching audit_raw_body
    (e.g. editing authDetails.username directly) -- these no longer match
    the original content embedded in audit_raw_body. This is the one a
    real attacker would actually try, since those are the fields anyone
    querying or viewing the dashboard relies on -- audit_raw_body itself
    is inert, nobody reads it directly. Confirmed necessary, not
    theoretical: an earlier version of this script only re-verified
    audit_hash against audit_raw_body and completely missed a live test
    tamper of authDetails.username, since audit_raw_body itself was left
    untouched.
  - audit_raw_body itself altered in place -> its hash no longer matches
    the stored audit_hash.
  - A document deleted -> a gap in the audit_seq sequence.
  - A document's stored hash/prev_hash fields themselves altered to hide
    the above -> the chain linkage (this doc's prev_hash == the previous
    doc's hash) breaks.
"""
from __future__ import annotations

import hashlib
import json
import os

import requests

OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "https://localhost:9200")
OPENSEARCH_ADMIN_PASSWORD = os.environ.get("OPENSEARCH_ADMIN_PASSWORD", "Poc-Multi-Org-Dev-2026!")
AUTH = ("admin", OPENSEARCH_ADMIN_PASSWORD)
GENESIS_HASH = "0" * 64


# Fields Vector's enrichment step legitimately adds *after* audit_chain_signer
# already computed the hash -- these are expected to differ from
# audit_raw_body's original content and must be excluded from the
# field-by-field comparison below, or every single enriched event would
# falsely report as "tampered".
ENRICHMENT_FIELDS = {"org_id", "org_name", "tenant"}
# Same idea, but for enrichment opensearch/vector.yaml writes *nested*
# inside a top-level field instead of alongside it -- keyed by the
# top-level field name to the set of its enrichment-added subkeys. Confirmed
# live (not theoretical): every active-org-switch event was flagging as
# "tampered" on `details` before this, because .details.new_active_
# organization_name / .details.previous_active_organization_name (added by
# vector.yaml's enrich_org_name transform) aren't in audit_raw_body's
# original `details`, exactly like org_id/org_name/tenant above but one
# level deeper.
NESTED_ENRICHMENT_FIELDS = {
    "details": {"new_active_organization_name", "previous_active_organization_name"},
}
# This script's own bookkeeping fields, likewise not part of the original
# Keycloak event.
AUDIT_FIELDS = {"audit_seq", "audit_prev_hash", "audit_hash", "audit_raw_body"}

PAGE_SIZE = 1000


def fetch_chained_events() -> list[dict]:
    # No _source filter -- unlike an earlier version of this script, the
    # full document is needed to cross-check every visible/queryable field
    # against what's embedded in audit_raw_body, not just the audit_*
    # bookkeeping fields themselves.
    #
    # Paginated via search_after on audit_seq (monotonic, unique -- a safe
    # sort key for this without needing a point-in-time), not a single
    # size:10000 request -- a single capped request would silently stop
    # verifying past the 10,000th event with no indication it had, which is
    # worse than erroring: a verifier that can silently stop verifying
    # defeats the point of it.
    events: list[tuple[str, dict]] = []
    search_after: int | None = None
    while True:
        query: dict = {
            "size": PAGE_SIZE,
            "query": {"exists": {"field": "audit_seq"}},
            "sort": [{"audit_seq": "asc"}],
        }
        if search_after is not None:
            query["search_after"] = [search_after]
        resp = requests.get(
            f"{OPENSEARCH_URL}/keycloak-events/_search",
            auth=AUTH,
            verify=False,
            timeout=15,
            json=query,
        )
        resp.raise_for_status()
        hits = resp.json()["hits"]["hits"]
        if not hits:
            break
        # (_id, _source) pairs -- Vector's id_key: "id" config
        # (opensearch/vector.yaml) promotes the original event's "id" field
        # to the document's actual OpenSearch _id and removes it from
        # _source entirely (confirmed empirically: _source.id is simply
        # absent, not None-valued) -- so "id" has to be checked against
        # _id specifically, not folded into the generic field-by-field
        # loop below.
        events.extend((h["_id"], h["_source"]) for h in hits)
        search_after = hits[-1]["_source"]["audit_seq"]
        if len(hits) < PAGE_SIZE:
            break
    return events


def main() -> None:
    events = fetch_chained_events()
    if not events:
        print("no chained events found -- nothing to verify")
        return

    problems: list[str] = []
    expected_seq = 1
    expected_prev_hash = GENESIS_HASH

    for doc_id, doc in events:
        seq = doc.get("audit_seq")
        if seq != expected_seq:
            problems.append(f"gap in sequence: expected audit_seq={expected_seq}, found {seq} -- a document may have been deleted")
            expected_seq = seq  # resync so we can keep checking the rest

        if doc.get("audit_prev_hash") != expected_prev_hash:
            problems.append(
                f"chain broken at audit_seq={seq}: audit_prev_hash={doc.get('audit_prev_hash')!r} "
                f"does not match the previous document's audit_hash={expected_prev_hash!r}"
            )

        recomputed = hashlib.sha256(
            doc.get("audit_prev_hash", "").encode() + doc.get("audit_raw_body", "").encode()
        ).hexdigest()
        if recomputed != doc.get("audit_hash"):
            problems.append(
                f"audit_raw_body tampered at audit_seq={seq}: recomputed hash {recomputed} "
                f"does not match stored audit_hash={doc.get('audit_hash')!r}"
            )

        # The check that actually matters in practice: does the document's
        # own top-level content (what anyone querying/viewing the dashboard
        # sees) still match what's embedded in audit_raw_body? Editing
        # authDetails.username (or any other original field) directly,
        # without touching audit_raw_body, passes the hash check above but
        # is caught here.
        try:
            original = json.loads(doc.get("audit_raw_body", ""))
        except json.JSONDecodeError:
            problems.append(f"audit_raw_body at audit_seq={seq} is not valid JSON -- cannot cross-check fields")
            original = {}

        # "id" is a special case -- Vector promotes it to the document's
        # actual _id and strips it from _source (see fetch_chained_events),
        # so it's checked against the real OpenSearch document id instead
        # of via the generic _source field loop below.
        if "id" in original and original["id"] != doc_id:
            problems.append(f"document identity tampered at audit_seq={seq}: OpenSearch _id={doc_id!r} but audit_raw_body has id={original['id']!r}")

        for key, original_value in original.items():
            if key == "id" or key in AUDIT_FIELDS or key in ENRICHMENT_FIELDS:
                continue
            live_value = doc.get(key)
            nested_exclusions = NESTED_ENRICHMENT_FIELDS.get(key)
            if nested_exclusions and isinstance(live_value, dict):
                live_value = {k: v for k, v in live_value.items() if k not in nested_exclusions}
            if live_value != original_value:
                problems.append(
                    f"field tampered at audit_seq={seq}: {key!r} is {doc.get(key)!r} "
                    f"but audit_raw_body has {original_value!r}"
                )

        expected_prev_hash = doc.get("audit_hash")
        expected_seq += 1

    print(f"checked {len(events)} chained events (audit_seq 1..{events[-1][1].get('audit_seq')})")
    if problems:
        print(f"\nTAMPERING DETECTED ({len(problems)} issue(s)):")
        for p in problems:
            print(f"  - {p}")
        raise SystemExit(1)
    print("chain intact -- no tampering detected")


if __name__ == "__main__":
    main()
