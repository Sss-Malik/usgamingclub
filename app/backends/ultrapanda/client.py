import json
import time

import httpx

from app.backends.base import BackendError, TransientBackendError
from app.backends.ultrapanda.crypto import (
    encrypt_login_cred,
    encrypt_xtoken,
    sign_body,
)
from app.backends.ultrapanda.errors import map_code
from app.backends.ultrapanda.session import CachedSession, TokenStore


FINGERPRINT = "45657e48dc42985f3e021fc065112c22"
"""Constant device fingerprint. Server doesn't validate (findings §7.4)."""

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "x-fingerprint": FINGERPRINT,
}


def _expired(session: CachedSession | None, *, skew_seconds: int = 60) -> bool:
    return session is None or session.expires_at - skew_seconds <= int(time.time())


class UltraPandaClient:
    """Auto-signed JSON-RPC client for the vpower (UltraPanda/VBlink) backend.

    Responsibilities: (1) token cache + DCL around login, (2) auto-inject `stime`+`sign`
    into every request body, (3) auto-inject `x-time`+`x-token`+`x-fingerprint` headers.
    """

    def __init__(
        self, *, base_url: str, username: str, password: str,
        http_client: httpx.AsyncClient,
        session_store: TokenStore,
        redis,
        game_id: int,
        session_ttl_seconds: int,
        throttle_ttl_seconds: int,
        throttle_acquire_timeout_seconds: float,
        session_lock_ttl_seconds: int,
        session_lock_acquire_timeout_seconds: float,
        driver_prefix: str,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._http = http_client
        self._store = session_store
        self._redis = redis
        self._game_id = game_id
        self._session_ttl = session_ttl_seconds
        self._throttle_ttl = throttle_ttl_seconds
        self._throttle_acquire = throttle_acquire_timeout_seconds
        self._lock_ttl = session_lock_ttl_seconds
        self._lock_acquire = session_lock_acquire_timeout_seconds
        self._driver = driver_prefix

    # ---- session ----

    async def get_or_login(self) -> str:
        cached = await self._store.get(self._game_id)
        if not _expired(cached):
            return cached.token       # type: ignore[union-attr]
        try:
            async with self._store.login_lock(
                self._game_id, ttl_seconds=self._lock_ttl,
                acquire_timeout=self._lock_acquire,
            ):
                cached = await self._store.get(self._game_id)
                if not _expired(cached):
                    return cached.token       # type: ignore[union-attr]
                return await self._do_login()
        except TimeoutError:
            return await self._do_login()

    async def _do_login(self) -> str:
        stime = int(time.time())
        body: dict = {
            "username": encrypt_login_cred(self._username, stime),
            "password": encrypt_login_cred(self._password, stime),
            "stime": stime,
            "auth_code": "",
        }
        body["sign"] = sign_body(body, stime)
        try:
            resp = await self._http.post(
                f"{self._base}/user/login",
                content=json.dumps(body).encode(),
                headers=_BASE_HEADERS,
            )
        except httpx.HTTPError as exc:
            raise TransientBackendError(
                f"{self._driver}:login_transport:{type(exc).__name__}"
            ) from exc
        if resp.status_code >= 500:
            raise TransientBackendError(f"{self._driver}:login_http_{resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise TransientBackendError(f"{self._driver}:login_bad_response") from exc
        code = data.get("code")
        if code == 20000:
            token = data.get("token")
            if not isinstance(token, str) or not token:
                raise TransientBackendError(f"{self._driver}:login_missing_token")
            await self._store.set(
                self._game_id,
                CachedSession(token=token, expires_at=int(time.time()) + self._session_ttl),
                ttl_seconds=self._session_ttl,
            )
            return token
        mapped = map_code(int(code) if isinstance(code, int) else 0, op="login")
        if mapped is None:
            raise BackendError(f"{self._driver}:login_failed")
        slug, terminal = mapped
        if terminal:
            raise BackendError(f"{self._driver}:{slug}")
        raise TransientBackendError(f"{self._driver}:{slug}")

    # ---- signed call ----

    async def call(self, path: str, params: dict | None = None) -> dict:
        """Make one signed POST. Body gets auto-signed; headers carry x-time/x-token/x-fingerprint."""
        token = await self.get_or_login()
        return await self._do_call(path, params or {}, token=token)

    async def _do_call(self, path: str, params: dict, *, token: str) -> dict:
        body: dict = dict(params)
        stime = int(time.time())
        body["stime"] = stime
        body["sign"] = sign_body(body, stime)
        ms_time = int(time.time() * 1000)
        x_token = encrypt_xtoken(token, ms_time)
        headers = {
            **_BASE_HEADERS,
            "x-time": str(ms_time),
            "x-token": x_token,
        }
        url = f"{self._base}{path}" if path.startswith("/") else f"{self._base}/{path}"
        try:
            resp = await self._http.post(url, content=json.dumps(body).encode(), headers=headers)
        except httpx.HTTPError as exc:
            raise TransientBackendError(
                f"{self._driver}:transport:{type(exc).__name__}"
            ) from exc
        if resp.status_code >= 500:
            raise TransientBackendError(f"{self._driver}:http_{resp.status_code}")
        if resp.status_code >= 400:
            raise TransientBackendError(f"{self._driver}:http_{resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise TransientBackendError(f"{self._driver}:bad_response") from exc
