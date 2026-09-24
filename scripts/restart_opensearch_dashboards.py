"""Restarts the opensearch-dashboards container after the "platform" realm
exists (see provision_platform_realm.py, earlier in setup.py's STEPS).

Needed because of a real startup-ordering problem, not a config bug:
opensearch-dashboards' OIDC plugin does a one-time synchronous fetch of
Keycloak's discovery document at container-boot time and crashes fatally
(no retry) if it 404s -- which it always will on a genuinely fresh
`docker compose up -d`, since the "platform" realm doesn't exist until this
script's provisioning steps create it, well after Dashboards' own container
has already started and tried (and failed) once. Restarting here, right
after the realm exists but still early in STEPS, gives it a real second
attempt with plenty of buffer before provision_dashboards.py (the last
step) needs a working Dashboards to provision saved objects against -- see
docs/opensearch-auth.md.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).parent.parent
DASHBOARDS_URL = os.environ.get("OPENSEARCH_DASHBOARDS_URL", "http://localhost:5601")


def main() -> None:
    # Only the FIRST setup.py run after a fresh `docker compose up -d` hits
    # the fatal-crash case described in this module's docstring -- a re-run
    # against an already-up Dashboards (e.g. `python3 scripts/setup.py`
    # twice in a row without `docker compose down -v` in between) has no
    # reason to restart it. Probed via HTTP rather than `docker inspect`'s
    # health status, since this service has no Docker-level healthcheck of
    # its own.
    try:
        if requests.get(f"{DASHBOARDS_URL}/api/status", timeout=5).ok:
            print("opensearch-dashboards already up, skipping restart")
            return
    except requests.RequestException:
        pass
    print("restarting opensearch-dashboards now that the platform realm exists ...")
    subprocess.run(["docker", "compose", "restart", "opensearch-dashboards"], cwd=REPO_ROOT, check=True)


if __name__ == "__main__":
    main()
