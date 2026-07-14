import time
from urllib.parse import urlencode

import httpx

from app.backends.base import BackendError, TransientBackendError
from app.backends.diagnostics import NULL_RECORDER
from app.backends.yolo.errors import looks_like_auth_failure, map_envelope
from app.backends.yolo.parsers import looks_like_login_page, parse_csrf_token
from app.backends.yolo.session import CachedSession, SessionStore

_FORM_CT = "application/x-www-form-urlencoded; charset=UTF-8"
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _expired(session: CachedSession | None, *, skew_seconds: int = 60) -> bool:
    return session is None or session.expires_at - skew_seconds <= int(time.time())


def _str_map(d: dict) -> dict[str, str]:
    return {k: ("" if v is None else str(v)) for k, v in d.items()}


class YoloClient:
    """Session-cookie + CSRF client for the YOLO777 Dcat admin panel."""

    def __init__(
        self, *, base_url: str, username: str, password: str,
        http_client: httpx.AsyncClient, session_store: SessionStore, game_id: int,
        session_ttl_seconds: int = 1800,
        login_lock_ttl_seconds: int = 10,
        login_lock_acquire_timeout_seconds: float = 10.0,
        diagnostics=None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._http = http_client
        self._store = session_store
        self._game_id = game_id
        self._ttl = session_ttl_seconds
        self._lock_ttl = login_lock_ttl_seconds
        self._lock_timeout = login_lock_acquire_timeout_seconds
        self._diag = diagnostics or NULL_RECORDER

    # ---- session ----

    async def get_session(self, *, invalidate: CachedSession | None = None) -> CachedSession:
        cached = await self._store.get(self._game_id)
        if cached and cached != invalidate and not _expired(cached):
            self._diag.session_event("hit")
            return cached
        async with self._store.login_lock(
            self._game_id, ttl_seconds=self._lock_ttl, acquire_timeout=self._lock_timeout,
        ):
            cached = await self._store.get(self._game_id)
            if cached and cached != invalidate and not _expired(cached):
                self._diag.session_event("hit")
                return cached
            session = await self._do_login()
            self._diag.session_event("fresh")
            await self._store.set(self._game_id, session, ttl_seconds=self._ttl)
            return session

    async def _do_login(self) -> CachedSession:
        cookies: dict[str, str] = {}
        # 1. GET login page -> scrape _token + collect cookies (XSRF-TOKEN).
        async with self._diag.step("login.page", phase="auth"):
            r1 = await self._get(f"{self._base}/admin/auth/login", cookies=cookies)
        cookies.update({k: v for k, v in r1.cookies.items()})
        token = parse_csrf_token(r1.text)
        # 2. POST credentials.
        body = urlencode({"_token": token, "username": self._username, "password": self._password})
        async with self._diag.step("login.submit", phase="auth"):
            r2 = await self._post(
                f"{self._base}/admin/auth/login", body, cookies=cookies, csrf=token,
            )
        cookies.update({k: v for k, v in r2.cookies.items()})
        # 3. Load an admin page to confirm auth + grab the per-session CSRF token.
        async with self._diag.step("login.confirm", phase="auth"):
            r3 = await self._get(f"{self._base}/admin/player_list", cookies=cookies)
        cookies.update({k: v for k, v in r3.cookies.items()})
        if looks_like_login_page(r3.text):
            raise BackendError("yolo:login_failed")
        csrf = parse_csrf_token(r3.text)
        return CachedSession(cookies=cookies, csrf_token=csrf, expires_at=int(time.time()) + self._ttl)

    # ---- requests ----

    async def post_form(self, path: str, fields: dict, *,
                        step: str = "primary", phase: str = "primary") -> dict:
        session = await self.get_session()
        async with self._diag.step(step, phase=phase):
            resp = await self._authed_post(path, fields, session)
        if looks_like_auth_failure(resp.status_code, resp.headers.get("Location", ""), _safe_text(resp)):
            async with self._diag.step("recovery", phase="recovery"):
                session = await self.get_session(invalidate=session)
                self._diag.session_event("relogin")
                resp = await self._authed_post(path, fields, session)
            if looks_like_auth_failure(resp.status_code, resp.headers.get("Location", ""), _safe_text(resp)):
                raise BackendError("yolo:auth_failed")
        return map_envelope(resp.status_code, _json_or_none(resp))

    async def get_text(self, path: str, params: dict | None = None, *,
                       step: str = "primary", phase: str = "primary") -> str:
        session = await self.get_session()
        async with self._diag.step(step, phase=phase):
            resp = await self._authed_get(path, params, session)
        if looks_like_auth_failure(resp.status_code, resp.headers.get("Location", ""), _safe_text(resp)):
            async with self._diag.step("recovery", phase="recovery"):
                session = await self.get_session(invalidate=session)
                self._diag.session_event("relogin")
                resp = await self._authed_get(path, params, session)
            if looks_like_auth_failure(resp.status_code, resp.headers.get("Location", ""), _safe_text(resp)):
                raise BackendError("yolo:auth_failed")
        if resp.status_code >= 500:
            raise TransientBackendError(f"yolo:http_{resp.status_code}")
        if resp.status_code >= 300:
            raise BackendError(f"yolo:http_{resp.status_code}")
        return resp.text

    async def _authed_post(self, path: str, fields: dict, session: CachedSession) -> httpx.Response:
        merged = {**_str_map(fields), "_token": session.csrf_token}
        return await self._post(f"{self._base}{path}", urlencode(merged),
                                cookies=session.cookies, csrf=session.csrf_token)

    async def _authed_get(self, path: str, params: dict | None, session: CachedSession) -> httpx.Response:
        return await self._get(f"{self._base}{path}", cookies=session.cookies,
                               params=_str_map(params or {}), csrf=session.csrf_token)

    # ---- transport ----

    async def _get(self, url: str, *, cookies: dict, params: dict | None = None,
                   csrf: str | None = None) -> httpx.Response:
        headers = {**_BROWSER_HEADERS, "X-Requested-With": "XMLHttpRequest"}
        if csrf:
            headers["X-CSRF-TOKEN"] = csrf
        try:
            return await self._http.get(url, params=params, headers=headers,
                                        cookies=cookies, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"yolo:transport:{type(exc).__name__}") from exc

    async def _post(self, url: str, body: str, *, cookies: dict, csrf: str) -> httpx.Response:
        headers = {
            **_BROWSER_HEADERS,
            "Content-Type": _FORM_CT, "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest", "X-CSRF-TOKEN": csrf,
        }
        try:
            return await self._http.post(url, content=body.encode(), headers=headers,
                                         cookies=cookies, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"yolo:transport:{type(exc).__name__}") from exc


def _json_or_none(resp: httpx.Response) -> dict | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _safe_text(resp: httpx.Response) -> str:
    try:
        return resp.text
    except Exception:  # noqa: BLE001
        return ""
