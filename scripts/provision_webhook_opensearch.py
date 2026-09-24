"""Enables the phasetwo ext-event-webhook listener on the "platform" realm and
registers two webhook targets, both container-internal hostnames Keycloak calls
server-to-server (same reasoning as KC_INTERNAL_URL, see README):

  - audit-chain-signer:2023/webhook   attaches a tamper-evident hash chain
                                       link (see docs/audit-log-integrity.md)
                                       then forwards into Vector -> OpenSearch.
                                       NOT vector:2021 directly anymore --
                                       audit-chain-signer must see every event
                                       first to chain it.
  - org-cache-watcher:2022/webhook    watches ORGANIZATION admin events to keep
                                       Vector's org id->name enrichment table
                                       (opensearch/org-lookup.csv) in sync

Both are registered with authType "hmac" -- confirmed against the extension's
actual source/docs (p2-inc/keycloak-events) and empirically against a real
signed request that the signature is lowercase-hex HMAC-SHA256 of the raw
request body, sent as X-Keycloak-Signature. These endpoints previously had
authType "none": anything on the docker network could POST fabricated events
into either one. See docs/opensearch-auth.md.

"bearer" (a short-lived JWT signed by the realm's own key, verified against
its JWKS -- no shared secret at all) was considered and would fit this
project's "use Keycloak identity" theme better, but neither receiver can
actually verify it: Vector's generic http_server source/VRL has no
RSA/JWKS verification capability, and adding that to org_cache_watcher.py
would mean giving up its "stdlib only, no build step" design (see its
docstring). HMAC is a deliberate, scoped exception, the same reasoning
already applied to Vector's own vector_writer OpenSearch credential.
"""
from __future__ import annotations

import state
from keycloak_admin import AdminClient, PLATFORM_REALM, admin_token

# Matches docker-compose.yml's WEBHOOK_HMAC_SECRET_CHAIN / WEBHOOK_HMAC_SECRET
# env vars exactly -- dev-only fixed values, same pattern as this project's
# other hardcoded dev secrets (product-app-secret-dev-only etc.).
WEBHOOK_TARGETS = {
    "audit-chain-signer.webhook.id": ("http://audit-chain-signer:2023/webhook", "Webhook-Hmac-Chain-Dev-2026!"),
    "org-cache-watcher.webhook.id": ("http://org-cache-watcher:2022/webhook", "Webhook-Hmac-OrgCache-Dev-2026!"),
}


def main() -> None:
    admin = AdminClient(token=admin_token())

    # ext-event-webhook must be added to the realm's eventsListeners, not replace
    # them. PUT is a full replacement, so fetch-mutate-PUT (see README gotchas).
    config = admin.get(f"/admin/realms/{PLATFORM_REALM}/events/config").json()
    listeners = sorted(set(config.get("eventsListeners", ["jboss-logging"])) | {"ext-event-webhook"})
    config.update(eventsListeners=listeners, eventsEnabled=True, adminEventsEnabled=True, adminEventsDetailsEnabled=True)
    admin.put(f"/admin/realms/{PLATFORM_REALM}/events/config", json=config)
    print(f"enabled ext-event-webhook + events logging on {PLATFORM_REALM}")

    existing = admin.get(f"/realms/{PLATFORM_REALM}/webhooks").json()
    existing_by_url = {w["url"]: w["id"] for w in existing}

    for state_key, (url, secret) in WEBHOOK_TARGETS.items():
        payload = {
            "enabled": "true",
            "url": url,
            "eventTypes": ["*"],
            "authType": "hmac",
            "secret": secret,
            "algorithm": "HmacSHA256",
        }
        # Look up by state_key first, not just by URL: a webhook's target
        # URL can change across a script's own evolution (this one did --
        # vector:2021 directly -> audit-chain-signer:2023 in front of it).
        # Matching by URL alone would miss the already-provisioned webhook
        # entirely (it's registered under the OLD url) and create a second,
        # stale duplicate instead of updating it.
        if state.has(state_key):
            webhook_id = state.load(state_key)
            admin.put(f"/realms/{PLATFORM_REALM}/webhooks/{webhook_id}", json={**payload, "id": webhook_id})
            print(f"updated webhook (id={webhook_id}) -> {url} (authType=hmac)")
        elif url in existing_by_url:
            webhook_id = existing_by_url[url]
            admin.put(f"/realms/{PLATFORM_REALM}/webhooks/{webhook_id}", json={**payload, "id": webhook_id})
            print(f"updated webhook -> {url} (id={webhook_id}, authType=hmac)")
        else:
            resp = admin.post(f"/realms/{PLATFORM_REALM}/webhooks", json=payload)
            webhook_id = AdminClient.location_id(resp)
            print(f"registered webhook -> {url} (id={webhook_id}, authType=hmac)")
        state.save(state_key, webhook_id)


if __name__ == "__main__":
    main()
