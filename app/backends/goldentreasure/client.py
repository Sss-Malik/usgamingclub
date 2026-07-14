# app/backends/goldentreasure/client.py
import asyncio
import json
import time

import httpx

from app.backends.base import BackendError, TransientBackendError
from app.backends.diagnostics import NULL_RECORDER
from app.backends.goldentreasure.crypto import (
    aes_b64,
    login_aes_key,
    sign_body,
    xtoken_header,
)
from app.backends.goldentreasure.errors import map_response
from app.backends.goldentreasure.session import CachedSession, SessionStore

# Cloudflare-friendly browser header set. The findings doc emphasizes that without these the
# request gets HTTP 403 from CF before reaching the API.
_BROWSER_HEADERS_BASE = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://agent.goldentreasure.mobi",
    "Referer": "https://agent.goldentreasure.mobi/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}

# Authentication-failure codes that trigger a transparent relogin + retry once.
_AUTH_DEAD_CODES = {-3, -17, 52}


class GoldenTreasureClient:
    """Golden Treasure HTTP client: AES-encrypted login, signed JSON POSTs, per-request
    x-token rebuild, and per-game mutating-op throttle."""

    def __init__(
        self, *,
        base_url: str, username: str, password: str,
        http_client: httpx.AsyncClient,
        session_store: SessionStore,
        redis,                                             # raw redis client for the throttle
        game_id: int,
        fingerprint: str = "db3bb59096022abb85b4612d53387101",
        diagnostics=None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._http = http_client
        self._session = session_store
        self._redis = redis
        self._game_id = game_id
        self._fingerprint = fingerprint
        self._diag = diagnostics or NULL_RECORDER

    # ---- session management ----

    async def get_token(self, *, invalidate: str | None = None) -> str:
        """Return a valid session token. Concurrent tokens are allowed by Golden Treasure
        (findings §10) — no double-checked locking needed; one cache re-read under the lock
        is enough to prevent thundering-herd logins.
        """
        cached = await self._session.get(self._game_id)
        if cached and cached.token != invalidate:
            return cached.token
        async with self._session.login_lock(self._game_id, ttl_seconds=10, acquire_timeout=10.0):
            cached = await self._session.get(self._game_id)
            if cached and cached.token != invalidate:
                return cached.token
            token = await self._do_login()
            # Token expiry isn't returned; pick a generous TTL (24h). Relogin on -3/-17 anyway.
            await self._session.set(
                self._game_id,
                CachedSession(token=token, expires_at=int(time.time()) + 86400),
                ttl_seconds=86400,
            )
            return token

    async def _do_login(self) -> str:
        """POST /api/user/login with AES-encrypted credentials. No x-token (no session yet)."""
        stime = int(time.time())
        key = login_aes_key(stime)
        body = {
            "username": aes_b64(self._username.strip(), key),
            "password": aes_b64(self._password, key),
            "stime": stime,
            "auth_code": "",
        }
        sign, _ = sign_body(body, stime=stime)
        body_json = await self._post_raw("/api/user/login", {**body, "sign": sign}, authenticated=False)
        code = body_json.get("code")
        if code == 20000:
            token = body_json.get("token")
            if not isinstance(token, str) or not token:
                raise TransientBackendError("gtreasure:login_missing_token")
            return token
        if code in (30100, 30200, 30201):
            slug = {30100: "system_verify", 30200: "google_auth_bind", 30201: "google_auth_verify"}[code]
            raise BackendError(f"gtreasure:requires_operator_action_{slug}")
        reason, terminal = map_response(
            int(code) if isinstance(code, int) else 0,
            str(body_json.get("message", "")),
        )
        raise (BackendError if terminal else TransientBackendError)(reason)

    # ---- throttle (mutating ops only) ----

    async def _acquire_throttle(self) -> None:
        """SET NX gtreasure_throttle:{game_id} ex=5. Poll until acquired or 30s timeout."""
        key = f"gtreasure_throttle:{self._game_id}"
        deadline = time.monotonic() + 30.0
        while True:
            if await self._redis.set(key, b"1", nx=True, ex=5):
                return
            if time.monotonic() >= deadline:
                raise TransientBackendError("gtreasure:throttle_wait_timeout")
            await asyncio.sleep(0.5)

    # ---- HTTP transport ----

    async def _post_raw(self, path: str, body: dict, *, authenticated: bool) -> dict:
        """Send one POST. authenticated=True adds the x-token/x-time headers from body['token']."""
        raw = json.dumps(body, separators=(",", ":"))
        headers = dict(_BROWSER_HEADERS_BASE)
        headers["x-fingerprint"] = self._fingerprint
        if authenticated:
            x_time_ms = int(time.time() * 1000)
            token = body.get("token")
            headers["x-token"] = xtoken_header(str(token), x_time_ms)
            headers["x-time"] = str(x_time_ms)
        try:
            resp = await self._http.post(
                f"{self._base_url}{path}", content=raw.encode(), headers=headers,
            )
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"gtreasure:transport:{type(exc).__name__}") from exc
        if resp.status_code >= 500:
            raise TransientBackendError(f"gtreasure:http_{resp.status_code}")
        if resp.status_code >= 300:
            raise BackendError(f"gtreasure:http_{resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise TransientBackendError("gtreasure:bad_response") from exc

    # ---- authenticated call (relogin on -3/-17/52 + optional throttle) ----

    async def call(self, path: str, params: dict, *, throttle: bool = False) -> dict:
        if throttle:
            await self._acquire_throttle()
        token = await self.get_token()
        body = {**params, "token": token}
        sign, stime = sign_body(body)
        body_json = await self._post_raw(path, {**body, "sign": sign, "stime": stime}, authenticated=True)
        if body_json.get("code") in _AUTH_DEAD_CODES:
            fresh = await self.get_token(invalidate=token)
            body["token"] = fresh
            sign, stime = sign_body(body)
            body_json = await self._post_raw(path, {**body, "sign": sign, "stime": stime}, authenticated=True)
            if body_json.get("code") in _AUTH_DEAD_CODES:
                raise BackendError("gtreasure:auth_failed")
        if body_json.get("code") == 20000:
            return body_json
        reason, terminal = map_response(
            int(body_json.get("code", 0)) if isinstance(body_json.get("code"), int) else 0,
            str(body_json.get("message", "")),
        )
        raise (BackendError if terminal else TransientBackendError)(reason)
