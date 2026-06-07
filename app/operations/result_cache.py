# app/operations/result_cache.py
import json
from dataclasses import dataclass
from typing import Protocol


@dataclass
class CachedOutcome:
    status: str               # "succeeded" | "failed"
    result: dict | None       # present when succeeded
    reason: str | None        # present when failed


class ResultCache(Protocol):
    async def get(self, key: str) -> CachedOutcome | None: ...
    async def set(self, key: str, outcome: CachedOutcome, ttl_seconds: int) -> None: ...


class InMemoryResultCache:
    """Process-local cache for tests / single-process fallback."""

    def __init__(self) -> None:
        self._store: dict[str, CachedOutcome] = {}

    async def get(self, key: str) -> CachedOutcome | None:
        return self._store.get(key)

    async def set(self, key: str, outcome: CachedOutcome, ttl_seconds: int) -> None:
        self._store[key] = outcome


class RedisResultCache:
    """Redis-backed at-most-once outcome cache keyed by idempotency_key."""

    def __init__(self, redis) -> None:
        self._redis = redis

    @staticmethod
    def _key(key: str) -> str:
        return f"opresult:{key}"

    async def get(self, key: str) -> CachedOutcome | None:
        raw = await self._redis.get(self._key(key))
        if raw is None:
            return None
        d = json.loads(raw)
        return CachedOutcome(status=d["status"], result=d.get("result"), reason=d.get("reason"))

    async def set(self, key: str, outcome: CachedOutcome, ttl_seconds: int) -> None:
        raw = json.dumps(
            {"status": outcome.status, "result": outcome.result, "reason": outcome.reason}
        )
        await self._redis.set(self._key(key), raw, ex=ttl_seconds)
