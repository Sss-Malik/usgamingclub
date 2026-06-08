# app/backends/gameroom/client.py
import time
from urllib.parse import urlencode

import httpx

from app.backends.base import BackendError, TransientBackendError
from app.backends.gameroom.errors import map_response
from app.backends.gameroom.session import CachedSession, SessionStore

_FORM_CT = "application/x-www-form-urlencoded; charset=UTF-8"


def _expired(session: CachedSession | None, *, skew_seconds: int = 60) -> bool:
    return session is None or session.expires_at - skew_seconds <= int(time.time())


class GameroomClient:
    """Form-urlencoded HTTP client for Gameroom with JWT session caching + single-session-safe refresh."""

    def __init__(
        self, *, base_url: str, username: str, password: str,
        http_client: httpx.AsyncClient, session_store: SessionStore, game_id: int,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._http = http_client
        self._session = session_store
        self._game_id = game_id

    # ---- session management ----

    async def get_token(self, *, invalidate: str | None = None) -> str:
        """Return a valid JWT. Double-checked locking so concurrent workers don't both re-login.

        If `invalidate` is given and the cache still holds exactly that value (or is empty/expired),
        force a fresh login. If the cache already holds a different (presumably fresher) token,
        return it without logging in.
        """
        cached = await self._session.get(self._game_id)
        if cached and cached.token != invalidate and not _expired(cached):
            return cached.token
        async with self._session.login_lock(self._game_id, ttl_seconds=10, acquire_timeout=10.0):
            cached = await self._session.get(self._game_id)
            if cached and cached.token != invalidate and not _expired(cached):
                return cached.token
            token, expires_at = await self._do_login()
            ttl = max(60, expires_at - int(time.time()) - 60)
            await self._session.set(self._game_id, CachedSession(token=token, expires_at=expires_at), ttl_seconds=ttl)
            return token

    async def _do_login(self) -> tuple[str, int]:
        url = f"{self._base_url}/api/login"
        body = urlencode({"username": self._username, "password": self._password})   # captcha omitted (server ignores)
        try:
            resp = await self._http.post(
                url, content=body.encode(),
                headers={"Content-Type": _FORM_CT, "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"gameroom:login_transport:{type(exc).__name__}") from exc
        if resp.status_code >= 500:
            raise TransientBackendError(f"gameroom:login_http_{resp.status_code}")
        try:
            body_json = resp.json()
        except ValueError as exc:
            raise TransientBackendError("gameroom:login_bad_response") from exc
        sc = body_json.get("status_code")
        if sc == 200:
            token = body_json.get("token")
            exp = body_json.get("expires_time")
            if not isinstance(token, str) or not token or not isinstance(exp, int):
                raise TransientBackendError("gameroom:login_missing_token")
            return token, exp
        reason, terminal = map_response(int(sc) if isinstance(sc, int) else 0, str(body_json.get("message", "")))
        if not terminal:
            raise TransientBackendError(reason)
        raise BackendError(reason)

    # ---- request ----

    async def call(self, method: str, path: str, *,
                   fields: dict[str, str | int] | None = None,
                   params: dict[str, str | int] | None = None) -> dict:
        """Issue one request, transparently re-login + retry once on status_code:410."""
        token = await self.get_token()
        resp = await self._http_request(method, path, token, fields=fields, params=params)
        if self._is_410(resp):
            fresh = await self.get_token(invalidate=token)
            resp = await self._http_request(method, path, fresh, fields=fields, params=params)
            if self._is_410(resp):
                raise BackendError("gameroom:auth_failed")
        return self._classify(resp)

    async def call_raw(self, method: str, path: str, *,
                       fields: dict[str, str | int] | None = None,
                       params: dict[str, str | int] | None = None) -> dict:
        """Like .call() but returns the full envelope (use when `data` is a list, e.g. userList)."""
        token = await self.get_token()
        resp = await self._http_request(method, path, token, fields=fields, params=params)
        if self._is_410(resp):
            fresh = await self.get_token(invalidate=token)
            resp = await self._http_request(method, path, fresh, fields=fields, params=params)
            if self._is_410(resp):
                raise BackendError("gameroom:auth_failed")
        # Same HTTP + envelope error classification as call(); only difference is we return the
        # full envelope on success (so callers can read `data` even when it's a list, e.g. userList).
        body = self._parse_or_raise(resp)
        sc = body.get("status_code")
        if sc == 200:
            return body
        reason, terminal = map_response(int(sc) if isinstance(sc, int) else 0, str(body.get("message", "")))
        if not terminal:
            raise TransientBackendError(reason)
        raise BackendError(reason)

    async def _http_request(self, method: str, path: str, token: str, *,
                            fields=None, params=None) -> httpx.Response:
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            if method == "GET":
                return await self._http.get(url, params=_str_map(params or {}), headers=headers)
            body = urlencode(_str_map(fields or {}))
            headers["Content-Type"] = _FORM_CT
            return await self._http.post(url, content=body.encode(), headers=headers)
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"gameroom:transport:{type(exc).__name__}") from exc

    def _parse_or_raise(self, resp: httpx.Response) -> dict:
        """HTTP-status check + JSON parse; raise Transient/BackendError on the wrong shapes."""
        if resp.status_code >= 500:
            raise TransientBackendError(f"gameroom:http_{resp.status_code}")
        if resp.status_code >= 300:
            raise BackendError(f"gameroom:http_{resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise TransientBackendError("gameroom:bad_response") from exc

    def _classify(self, resp: httpx.Response) -> dict:
        body = self._parse_or_raise(resp)
        sc = body.get("status_code")
        if sc == 200:
            # `data` may be missing (e.g. agentWithdraw success). Top-level keys (token, money, etc.)
            # are also exposed so callers can read e.g. login's top-level money / agent/getMoney's
            # money fallback.
            data = body.get("data")
            if isinstance(data, dict):
                return data
            return {k: v for k, v in body.items() if k not in {"status_code", "message", "code", "data"}}
        reason, terminal = map_response(int(sc) if isinstance(sc, int) else 0, str(body.get("message", "")))
        if not terminal:
            raise TransientBackendError(reason)
        raise BackendError(reason)

    @staticmethod
    def _is_410(resp: httpx.Response) -> bool:
        if resp.status_code != 200:
            return False
        try:
            return resp.json().get("status_code") == 410
        except ValueError:
            return False


def _str_map(d: dict) -> dict[str, str]:
    return {k: ("" if v is None else str(v)) for k, v in d.items()}
