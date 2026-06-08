import time
from urllib.parse import urlencode

import httpx

from app.backends.base import BackendError, TransientBackendError
from app.backends._aspnet_cashier.errors import classify_business_failure_message
from app.backends._aspnet_cashier.login import _BASE_HEADERS, _FORM_CT, _SESSION_COOKIE, login
from app.backends._aspnet_cashier.parsers import (
    parse_agent_balance_widget,
    parse_dialog_response,
    parse_get_score_response,
    parse_milkyway_balance_row,
    parse_sentinel,
    parse_update_select,
    parse_viewstate,
)
from app.backends._aspnet_cashier.session import CachedSession, SessionStore
from app.captcha.base import CaptchaSolver


def _expired(session: CachedSession | None, *, skew_seconds: int = 60) -> bool:
    return session is None or session.expires_at - skew_seconds <= int(time.time())


class AspnetCashierClient:
    """HTTP client shared by OrionStars + MilkyWay backends.

    Responsibilities: (1) session cache + double-checked locking around login,
    (2) cookie+Accept-Language injection on every request, (3) session-death detection
    with retry-once-after-relogin, (4) the AccountsList/dialog helpers used by ops.
    """

    def __init__(
        self, *, base_url: str, username: str, password: str,
        http_client: httpx.AsyncClient,
        session_store: SessionStore,
        captcha_solver: CaptchaSolver,
        game_id: int,
        session_ttl_seconds: int,
        lock_ttl_seconds: int,
        lock_acquire_timeout_seconds: float,
        captcha_login_max_attempts: int,
        driver_prefix: str,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._http = http_client
        self._store = session_store
        self._captcha = captcha_solver
        self._game_id = game_id
        self._session_ttl = session_ttl_seconds
        self._lock_ttl = lock_ttl_seconds
        self._lock_acquire = lock_acquire_timeout_seconds
        self._max_attempts = captcha_login_max_attempts
        self._driver = driver_prefix

    # ---- session ----

    async def get_or_login(self) -> str:
        cached = await self._store.get(self._game_id)
        if not _expired(cached):
            return cached.cookie       # type: ignore[union-attr]
        try:
            async with self._store.login_lock(
                self._game_id, ttl_seconds=self._lock_ttl,
                acquire_timeout=self._lock_acquire,
            ):
                cached = await self._store.get(self._game_id)
                if not _expired(cached):
                    return cached.cookie       # type: ignore[union-attr]
                return await self._do_login()
        except TimeoutError:
            # Lock contention. The lock is efficiency-only here (sessions coexist), so
            # fall through to an unlocked login — a wasted captcha beats a failed op.
            return await self._do_login()

    async def _do_login(self) -> str:
        cookie = await login(
            http=self._http, base_url=self._base,
            username=self._username, password=self._password,
            captcha_solver=self._captcha,
            max_attempts=self._max_attempts,
            driver_prefix=self._driver,
        )
        await self._store.set(
            self._game_id,
            CachedSession(cookie=cookie, expires_at=int(time.time()) + self._session_ttl),
            ttl_seconds=self._session_ttl,
        )
        return cookie
