import asyncio

import pytest

from app.backends.yolo.session import CachedSession, InMemorySessionStore, RedisSessionStore


async def test_inmemory_set_get_clear():
    store = InMemorySessionStore()
    assert await store.get(1) is None
    s = CachedSession(cookies={"laravel_session": "abc"}, csrf_token="tok", expires_at=999)
    await store.set(1, s, ttl_seconds=60)
    got = await store.get(1)
    assert got.cookies == {"laravel_session": "abc"} and got.csrf_token == "tok"
    await store.clear(1)
    assert await store.get(1) is None


async def test_inmemory_login_lock_serializes():
    store = InMemorySessionStore()
    order = []

    async def worker(n):
        async with store.login_lock(1, acquire_timeout=2.0):
            order.append(("in", n))
            await asyncio.sleep(0.05)
            order.append(("out", n))

    await asyncio.gather(worker(1), worker(2))
    # No interleaving: each in/out pair is contiguous.
    assert order[0][0] == "in" and order[1][0] == "out"
    assert order[2][0] == "in" and order[3][0] == "out"


async def test_redis_roundtrip_and_lock(fake_redis):
    store = RedisSessionStore(fake_redis)
    s = CachedSession(cookies={"laravel_session": "abc", "XSRF-TOKEN": "x"}, csrf_token="tok", expires_at=123)
    await store.set(7, s, ttl_seconds=60)
    got = await store.get(7)
    assert got == s
    # Lock is exclusive while held.
    async with store.login_lock(7, ttl_seconds=5, acquire_timeout=0.3):
        with pytest.raises(TimeoutError):
            async with store.login_lock(7, ttl_seconds=5, acquire_timeout=0.3):
                pass
    await store.clear(7)
    assert await store.get(7) is None
