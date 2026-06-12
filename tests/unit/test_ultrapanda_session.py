import asyncio

import pytest

from app.backends.ultrapanda.session import (
    CachedSession,
    InMemoryTokenStore,
    RedisTokenStore,
)


async def test_in_memory_set_get_clear():
    store = InMemoryTokenStore()
    assert await store.get(1) is None
    await store.set(1, CachedSession(token="tok1", expires_at=9_999_999_999), ttl_seconds=60)
    got = await store.get(1)
    assert got is not None and got.token == "tok1"
    await store.clear(1)
    assert await store.get(1) is None


async def test_redis_set_get_clear_and_key_prefix(fake_redis):
    store = RedisTokenStore(fake_redis)
    await store.set(7, CachedSession(token="abc", expires_at=9_999_999_999), ttl_seconds=120)
    raw = await fake_redis.get("vpower_session:7")
    assert raw is not None and b"abc" in raw
    got = await store.get(7)
    assert got is not None and got.token == "abc" and got.expires_at == 9_999_999_999
    await store.clear(7)
    assert await store.get(7) is None


async def test_redis_set_respects_ttl(fake_redis):
    store = RedisTokenStore(fake_redis)
    await store.set(8, CachedSession(token="t", expires_at=9_999_999_999), ttl_seconds=1)
    ttl = await fake_redis.ttl("vpower_session:8")
    assert 0 < ttl <= 1


async def test_redis_login_lock_writes_and_clears_key(fake_redis):
    store = RedisTokenStore(fake_redis)
    async with store.login_lock(game_id=9, ttl_seconds=5):
        assert (await fake_redis.exists("vpower_session_lock:9")) == 1
    assert (await fake_redis.exists("vpower_session_lock:9")) == 0


async def test_redis_login_lock_setnx_blocks_second_acquire(fake_redis):
    store = RedisTokenStore(fake_redis)
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
    async with store.login_lock(game_id=10, ttl_seconds=30, poll_seconds=0.05, acquire_timeout=1.0):
        pass
