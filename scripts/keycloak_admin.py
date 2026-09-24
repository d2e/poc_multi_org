"""Shared Keycloak admin REST client for this project's provisioning scripts.

Host-facing config (KC_URL) vs container-internal config (KC_INTERNAL_URL) are
both read from the environment so the same module works unmodified from the
host (provisioning scripts) and from inside the compose network (services
like org_cache_watcher.py) -- see README "Why two URLs".
"""
from __future__ import annotations

import os
import time

import requests

KC_URL = os.environ.get("KC_URL", "http://localhost:8082")
KC_INTERNAL_URL = os.environ.get("KC_INTERNAL_URL", "http://localhost:8080")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin")
PLATFORM_REALM = "platform"

# Each tenant gets its own federated realm ({org}-idp), org in the platform
# realm, and email domain for Home IdP Discovery. "acme" additionally backs
# the subdomain-based routing demo (see docs/tenant-routing.md) -- its email
# domain (Home IdP Discovery, unchanged mechanism) is intentionally distinct
# from its product-app subdomain (kc_idp_hint routing, new mechanism).
TENANTS = [
    {"org": "org1", "domain": "org1.example.com", "display": "Org 1"},
    {"org": "org2", "domain": "org2.example.com", "display": "Org 2"},
    {"org": "org3", "domain": "org3.example.com", "display": "Org 3"},
    {"org": "acme", "domain": "acme.example.com", "display": "Acme"},
]


def admin_token(kc_url: str = KC_URL) -> str:
    resp = requests.post(
        f"{kc_url}/realms/master/protocol/openid-connect/token",
        data={
            "client_id": "admin-cli",
            "username": ADMIN_USER,
            "password": ADMIN_PASS,
            "grant_type": "password",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def service_account_token(
    client_id: str = "automation-svc",
    client_secret: str = "automation-svc-secret-dev-only",
    realm: str = "master",
    kc_url: str = KC_URL,
) -> str:
    """Client-credentials grant for the service account created by
    provision_service_account_client.py -- headless equivalent of
    admin_token() that doesn't need the master admin username+password."""
    resp = requests.post(
        f"{kc_url}/realms/{realm}/protocol/openid-connect/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def wait_for_keycloak(kc_url: str = KC_URL, timeout_s: int = 180) -> None:
    print(f"waiting for keycloak at {kc_url} ...")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if requests.get(f"{kc_url}/realms/master", timeout=5).ok:
                print("keycloak is up")
                return
        except requests.RequestException:
            pass
        time.sleep(3)
    raise SystemExit("keycloak did not become ready in time")


class AdminClient:
    """Thin requests.Session wrapper carrying the admin bearer token."""

    def __init__(self, kc_url: str = KC_URL, token: str | None = None):
        self.kc_url = kc_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token or admin_token(kc_url)}"

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.kc_url}{path}"

    def get(self, path: str, **kw) -> requests.Response:
        kw.setdefault("timeout", 10)
        r = self.session.get(self._url(path), **kw)
        r.raise_for_status()
        return r

    def post(self, path: str, **kw) -> requests.Response:
        kw.setdefault("timeout", 10)
        r = self.session.post(self._url(path), **kw)
        r.raise_for_status()
        return r

    def put(self, path: str, **kw) -> requests.Response:
        kw.setdefault("timeout", 10)
        r = self.session.put(self._url(path), **kw)
        r.raise_for_status()
        return r

    def delete(self, path: str, **kw) -> requests.Response:
        kw.setdefault("timeout", 10)
        r = self.session.delete(self._url(path), **kw)
        r.raise_for_status()
        return r

    def exists(self, path: str) -> bool:
        r = self.session.get(self._url(path), timeout=10)
        return r.status_code == 200

    @staticmethod
    def location_id(resp: requests.Response) -> str:
        """Extract the trailing id from a Keycloak 201's Location header."""
        return resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1]
