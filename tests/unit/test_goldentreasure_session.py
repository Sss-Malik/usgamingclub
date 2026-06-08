import asyncio

import pytest

from app.backends.goldentreasure.session import (
    CachedSession,
    InMemorySessionStore,
    RedisSessionStore,
)


async def test_in_memory_set_get_clear():
    store = InMemorySessionStore()
    assert await store.get(13) is None
    await store.set(13, CachedSession(token="t", expires_at=9_999_999_999), ttl_seconds=60)
    got = await store.get(13)
    assert got is not None and got.token == "t"
    await store.clear(13)
    assert await store.get(13) is None


async def test_redis_uses_gtreasure_session_key_prefix(fake_redis):
    # Spec GT5: keys must use `gtreasure_session:` / `gtreasure_login:` namespaces, NOT gameroom's.
    store = RedisSessionStore(fake_redis)
    await store.set(7, CachedSession(token="abc", expires_at=9_999_999_999), ttl_seconds=120)
    assert await fake_redis.get("gtreasure_session:7") is not None
    assert await fake_redis.get("gameroom_session:7") is None       # NOT in gameroom's namespace
    got = await store.get(7)
    assert got is not None and got.token == "abc"


async def test_redis_login_lock_uses_gtreasure_login_key_prefix(fake_redis):
    store = RedisSessionStore(fake_redis)
    async with store.login_lock(game_id=9, ttl_seconds=5):
        assert (await fake_redis.exists("gtreasure_login:9")) == 1
        assert (await fake_redis.exists("gameroom_login:9")) == 0
    assert (await fake_redis.exists("gtreasure_login:9")) == 0


async def test_redis_login_lock_setnx_blocks_second_acquire(fake_redis):
    store = RedisSessionStore(fake_redis)
    held = asyncio.Event()
    release = asyncio.Event()

    async def hold_lock():
        async with store.login_lock(game_id=10, ttl_seconds=30):
            held.set()
            await release.wait()

    task = asyncio.create_task(hold_lock())
    await held.wait()
    with pytest.raises(TimeoutError):
        async with store.login_lock(game_id=10, ttl_seconds=30, poll_seconds=0.05, acquire_timeout=0.2):
            raise AssertionError("should not have acquired lock")
    release.set()
    await task
    # Lock released — next acquire succeeds immediately.
    async with store.login_lock(game_id=10, ttl_seconds=30, poll_seconds=0.05, acquire_timeout=1.0):
        pass
