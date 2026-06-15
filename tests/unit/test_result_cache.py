# tests/unit/test_result_cache.py
from app.operations.result_cache import CachedOutcome, InMemoryResultCache, RedisResultCache


async def test_in_memory_get_set():
    cache = InMemoryResultCache()
    assert await cache.get("k") is None
    await cache.set("k", CachedOutcome("succeeded", {"balance": 1}, None), 900)
    got = await cache.get("k")
    assert got.status == "succeeded" and got.result == {"balance": 1}


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.ttls = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.ttls[key] = ex


async def test_redis_roundtrip_and_ttl_and_key():
    fake = FakeRedis()
    cache = RedisResultCache(fake)
    await cache.set("idem-9", CachedOutcome("failed", None, "backend_error: x"), 900)
    assert "opresult:idem-9" in fake.store
    assert fake.ttls["opresult:idem-9"] == 900
    got = await cache.get("idem-9")
    assert got.status == "failed" and got.reason == "backend_error: x" and got.result is None
    assert await cache.get("missing") is None
