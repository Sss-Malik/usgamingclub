import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CachedSession:
    cookie: str           # ASP.NET_SessionId value
    expires_at: int       # unix seconds (when we consider this session stale; not the server's TTL)


class SessionStore(Protocol):
    async def get(self, game_id: int) -> CachedSession | None: ...
    async def set(self, game_id: int, session: CachedSession, ttl_seconds: int) -> None: ...
    async def clear(self, game_id: int) -> None: ...

    def login_lock(
        self, game_id: int, *, ttl_seconds: int = 20,
        poll_seconds: float = 0.1, acquire_timeout: float = 30.0,
    ):
        """Async context manager that serializes login flows for a game across workers.

        For OrionStars/MilkyWay the lock is purely an efficiency measure (one captcha solve
        serves all concurrent waiters); concurrent valid sessions for the same agent coexist
        on these portals — see Phase 5 design doc, "Open questions".

        Raises TimeoutError if the lock cannot be acquired within acquire_timeout.
        """


class InMemoryCookieSessionStore:
    """Process-local store with per-game asyncio.Lock. For tests / single-process fallback."""

    def __init__(self) -> None:
        self._store: dict[int, CachedSession] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def get(self, game_id: int) -> CachedSession | None:
        return self._store.get(game_id)

    async def set(self, game_id: int, session: CachedSession, ttl_seconds: int) -> None:
        self._store[game_id] = session

    async def clear(self, game_id: int) -> None:
        self._store.pop(game_id, None)

    @asynccontextmanager
    async def login_lock(self, game_id: int, *, ttl_seconds: int = 20,
                         poll_seconds: float = 0.1, acquire_timeout: float = 30.0):
        lock = self._locks.setdefault(game_id, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), timeout=acquire_timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"aspnet login lock acquire timeout (game_id={game_id})") from exc
        try:
            yield
        finally:
            lock.release()


def _key_session(game_id: int) -> str:
    return f"aspnet_session:{game_id}"


def _key_lock(game_id: int) -> str:
    return f"aspnet_login:{game_id}"


class CookieSessionStore:
    """Redis-backed cookie session store + SET NX login lock. Shared across all workers."""

    def __init__(self, redis) -> None:
        self._redis = redis

    async def get(self, game_id: int) -> CachedSession | None:
        raw = await self._redis.get(_key_session(game_id))
        if raw is None:
            return None
        d = json.loads(raw)
        return CachedSession(cookie=d["cookie"], expires_at=int(d["expires_at"]))

    async def set(self, game_id: int, session: CachedSession, ttl_seconds: int) -> None:
        raw = json.dumps({"cookie": session.cookie, "expires_at": session.expires_at})
        await self._redis.set(_key_session(game_id), raw, ex=max(1, ttl_seconds))

    async def clear(self, game_id: int) -> None:
        await self._redis.delete(_key_session(game_id))

    @asynccontextmanager
    async def login_lock(self, game_id: int, *, ttl_seconds: int = 20,
                         poll_seconds: float = 0.1, acquire_timeout: float = 30.0):
        key = _key_lock(game_id)
        deadline = time.monotonic() + acquire_timeout
        acquired = False
        while True:
            res = await self._redis.set(key, b"1", nx=True, ex=ttl_seconds)
            if res:
                acquired = True
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"aspnet login lock acquire timeout (game_id={game_id})")
            await asyncio.sleep(poll_seconds)
        try:
            yield
        finally:
            if acquired:
                try:
                    await self._redis.delete(key)
                except Exception:  # noqa: BLE001 - best-effort release; TTL backs us up
                    pass
