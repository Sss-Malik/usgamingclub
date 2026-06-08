import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CachedSession:
    token: str
    expires_at: int          # unix seconds (when the JWT itself expires)


class SessionStore(Protocol):
    async def get(self, game_id: int) -> CachedSession | None: ...
    async def set(self, game_id: int, session: CachedSession, ttl_seconds: int) -> None: ...
    async def clear(self, game_id: int) -> None: ...

    def login_lock(
        self, game_id: int, *, ttl_seconds: int = 10,
        poll_seconds: float = 0.1, acquire_timeout: float = 10.0,
    ):
        """Async context manager that serializes /api/login calls for a game across workers.

        Raises asyncio.TimeoutError if the lock cannot be acquired within acquire_timeout.
        """


class InMemorySessionStore:
    """Process-local store with an in-process asyncio.Lock per game. For tests / single-process fallback."""

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
    async def login_lock(self, game_id: int, *, ttl_seconds: int = 10,
                         poll_seconds: float = 0.1, acquire_timeout: float = 10.0):
        lock = self._locks.setdefault(game_id, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), timeout=acquire_timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"gameroom login lock acquire timeout (game_id={game_id})") from exc
        try:
            yield
        finally:
            lock.release()


def _key_session(game_id: int) -> str:
    return f"gameroom_session:{game_id}"


def _key_lock(game_id: int) -> str:
    return f"gameroom_login:{game_id}"


class RedisSessionStore:
    """Redis-backed session store + SET NX login lock. Shared across all workers."""

    def __init__(self, redis) -> None:
        self._redis = redis

    async def get(self, game_id: int) -> CachedSession | None:
        raw = await self._redis.get(_key_session(game_id))
        if raw is None:
            return None
        d = json.loads(raw)
        return CachedSession(token=d["token"], expires_at=int(d["expires_at"]))

    async def set(self, game_id: int, session: CachedSession, ttl_seconds: int) -> None:
        raw = json.dumps({"token": session.token, "expires_at": session.expires_at})
        await self._redis.set(_key_session(game_id), raw, ex=max(1, ttl_seconds))

    async def clear(self, game_id: int) -> None:
        await self._redis.delete(_key_session(game_id))

    @asynccontextmanager
    async def login_lock(self, game_id: int, *, ttl_seconds: int = 10,
                         poll_seconds: float = 0.1, acquire_timeout: float = 10.0):
        key = _key_lock(game_id)
        deadline = time.monotonic() + acquire_timeout
        acquired = False
        while True:
            # SET NX with TTL: only succeeds if the key is absent. Returns True / b'OK' / None.
            res = await self._redis.set(key, b"1", nx=True, ex=ttl_seconds)
            if res:
                acquired = True
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"gameroom login lock acquire timeout (game_id={game_id})")
            await asyncio.sleep(poll_seconds)
        try:
            yield
        finally:
            if acquired:
                try:
                    await self._redis.delete(key)
                except Exception:  # noqa: BLE001 - best-effort release; TTL backs us up
                    pass
