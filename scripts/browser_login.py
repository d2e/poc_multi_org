"""Shared browser-simulation helpers for driving Keycloak's real login
HTML/redirect flow from Python (not just calling its JSON APIs). Used by
both scripts/verify.py (testing) and provision_globex_tenant.py
(bootstrapping a federated user's platform account before granting it
additional org memberships -- see that file's docstring for why).
"""
from __future__ import annotations

import html
import http.cookies
import re
from urllib.parse import urlparse

import requests

FORM_ACTION_RE = re.compile(r'action="([^"]*)"')


class PlainCookieJarSession:
    """Manual, path-scoped cookie jar instead of requests.Session's built-in one.

    Two independent problems with requests.Session here, both specific to
    driving Keycloak's actual login HTML/redirect flow (not just calling its
    JSON APIs):

    1. Keycloak sets Secure on its session cookies even over plain http in
       dev mode. requests re-derives a fresh, default-policy
       cookiejar.CookieJar every time it prepares a request
       (merge_cookies(RequestsCookieJar(), self.cookies) in
       Session.prepare_request), so overriding the session jar's policy has
       no effect on outgoing requests -- only on what gets accepted into
       storage. The Secure cookie is silently never sent back, breaking the
       flow with "Restart login cookie not found".

    2. This flow crosses two realms (platform, org3-idp) that each set
       same-named session cookies (AUTH_SESSION_ID, KC_RESTART, ...) scoped
       to different paths (/realms/platform/ vs /realms/org3-idp/). A jar
       that isn't path-scoped clobbers platform's session cookies with
       org3-idp's the moment you visit the org3-idp login page, so the
       broker callback back on platform can't resume its own flow
       ("An error occurred, please login again through your application").

    KC_URL is always local/plain HTTP for this project, so just track
    cookies per-path by hand rather than fighting requests' cookie jar.
    """

    def __init__(self) -> None:
        self._cookies_by_path: dict[str, dict[str, str]] = {}

    def _cookies_for(self, path: str) -> dict[str, str]:
        merged: dict[str, str] = {}
        for cookie_path, cookies in self._cookies_by_path.items():
            if path.startswith(cookie_path):
                merged.update(cookies)
        return merged

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        kw["cookies"] = self._cookies_for(urlparse(url).path)
        resp = requests.request(method, url, **kw)
        for cookie in resp.cookies:
            self._cookies_by_path.setdefault(cookie.path, {})[cookie.name] = cookie.value
        # requests' cookiejar silently drops already-expired cookies from
        # resp.cookies entirely (that's how a server tells a client "delete
        # this" -- Max-Age=0 / Expires in the past), so the loop above never
        # sees deletions. Parse the raw Set-Cookie headers ourselves to
        # actually remove them -- otherwise this jar would keep resending a
        # stale session cookie forever after logout, silently masking a real
        # logout bug as "still logged in".
        for raw in resp.raw.headers.get_all("Set-Cookie") if resp.raw else []:
            parsed = http.cookies.SimpleCookie()
            parsed.load(raw)
            for name, morsel in parsed.items():
                if morsel["max-age"] == "0" or (morsel["expires"] and "1970" in morsel["expires"]):
                    self._cookies_by_path.get(morsel["path"] or "/", {}).pop(name, None)
        return resp

    def get(self, url: str, **kw) -> requests.Response:
        return self._request("GET", url, **kw)

    def post(self, url: str, **kw) -> requests.Response:
        return self._request("POST", url, **kw)


def form_action(page_html: str) -> str:
    return html.unescape(FORM_ACTION_RE.search(page_html).group(1))


def tenant_get(
    session: PlainCookieJarSession,
    url: str,
    tenant_host: str,
    product_app_url: str = "http://127.0.0.1:3000",
    **kw,
) -> requests.Response:
    """GET against a product-app tenant subdomain, rewriting the tenant
    hostname to 127.0.0.1 and setting it as the Host header instead -- lets
    this run without relying on nip.io's DNS actually resolving (it may
    not, e.g. in a network-restricted sandbox; a real browser on a normal
    machine resolves it fine, this is just for script portability).
    """
    if tenant_host in url:
        url = url.replace(f"http://{tenant_host}", product_app_url)
    if url.startswith(product_app_url):
        kw.setdefault("headers", {})["Host"] = tenant_host
    return session.get(url, **kw)
