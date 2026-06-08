# tests/unit/test_goldentreasure_client.py
import asyncio
import json
import time

import httpx
import pytest
import respx

from app.backends.base import BackendError, TransientBackendError
from app.backends.goldentreasure.client import GoldenTreasureClient
from app.backends.goldentreasure.crypto import xtoken_header
from app.backends.goldentreasure.session import CachedSession, InMemorySessionStore

BASE = "https://gt.test"


def _make_client(http, *, store=None, fake_redis=None):
    return GoldenTreasureClient(
        base_url=BASE, username="Test02Gd1WEB", password="Zaeem@1233",
        http_client=http,
        session_store=store or InMemorySessionStore(),
        redis=fake_redis,
        game_id=13,
    )


def _login_ok(token="Ttok"):
    return {"code": 20000, "name": "Test02Gd1WEB", "token": token,
            "frame": 0, "data": {}}


# ---- login ----

@respx.mock
async def test_login_posts_aes_encrypted_creds_with_matching_stime(monkeypatch):
    monkeypatch.setattr("app.backends.goldentreasure.client.time.time", lambda: 1779281935.0)
    route = respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    async with httpx.AsyncClient() as http:
        token = await _make_client(http).get_token()
    assert token == "Ttok"
    sent_body = json.loads(route.calls.last.request.content.decode())
    # AES oracles from findings §4
    assert sent_body["username"] == "BXrmQgZgqwThh5+CjFOLFA=="
    assert sent_body["password"] == "suyUHuDw+rXOKpJvvW7WsA=="
    assert sent_body["stime"] == 1779281935
    assert sent_body["auth_code"] == ""
    assert "sign" in sent_body
    # No x-token / x-time on login
    headers = {k.lower(): v for k, v in route.calls.last.request.headers.items()}
    assert "x-token" not in headers and "x-time" not in headers
    # Cloudflare-friendly headers present
    assert headers["origin"] == "https://agent.goldentreasure.mobi"
    assert "chrome" in headers["user-agent"].lower()


@respx.mock
async def test_login_30100_is_terminal_operator_action():
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(
        200, json={"code": 30100, "message": "Verify code required"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _make_client(http).get_token()
    assert ei.value.reason == "gtreasure:requires_operator_action_system_verify"
    assert not isinstance(ei.value, TransientBackendError)


@respx.mock
async def test_login_30200_is_terminal_google_auth():
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(
        200, json={"code": 30200, "message": "Google Auth required"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _make_client(http).get_token()
    assert ei.value.reason == "gtreasure:requires_operator_action_google_auth_bind"


@respx.mock
async def test_login_5xx_is_transient():
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _make_client(http).get_token()


# ---- get_token reuse + invalidate ----

@respx.mock
async def test_get_token_returns_cached_when_present():
    route = respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("would_be_new")))
    store = InMemorySessionStore()
    await store.set(13, CachedSession(token="cached", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    async with httpx.AsyncClient() as http:
        token = await _make_client(http, store=store).get_token()
    assert token == "cached"
    assert route.call_count == 0


@respx.mock
async def test_get_token_with_invalidate_logs_in_when_cache_holds_dead_token():
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("Tnew")))
    store = InMemorySessionStore()
    await store.set(13, CachedSession(token="Tdead", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    async with httpx.AsyncClient() as http:
        token = await _make_client(http, store=store).get_token(invalidate="Tdead")
    assert token == "Tnew"


# ---- call() success + x-token + sign ----

@respx.mock
async def test_call_success_attaches_xtoken_xtime_and_signs(monkeypatch):
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("Ttok")))
    route = respx.post(f"{BASE}/api/user/CurScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "LimitNum": "20.00"}))
    # Freeze time so we can predict x-time / sign.
    monkeypatch.setattr("app.backends.goldentreasure.client.time.time", lambda: 1779281936.505)
    async with httpx.AsyncClient() as http:
        data = await _make_client(http).call("/api/user/CurScore", {})
    assert data["LimitNum"] == "20.00"
    sent = route.calls.last.request
    headers = {k.lower(): v for k, v in sent.headers.items()}
    # x-time = int(time.time()*1000)
    assert headers["x-time"] == "1779281936505"
    # x-token = url-encoded AES of the session token with key f"xtu{x_time_ms}"
    assert headers["x-token"] == xtoken_header("Ttok", 1779281936505)
    body = json.loads(sent.content.decode())
    assert body["token"] == "Ttok"
    assert "sign" in body and "stime" in body


# ---- call() relogin on -3/-17/52 ----

@respx.mock
async def test_call_code_minus3_relogins_and_retries_once_successfully():
    respx.post(f"{BASE}/api/user/login").mock(side_effect=[
        httpx.Response(200, json=_login_ok("Told")),
        httpx.Response(200, json=_login_ok("Tnew")),
    ])
    respx.post(f"{BASE}/api/user/CurScore").mock(side_effect=[
        httpx.Response(200, json={"code": -3, "message": "token invalid"}),
        httpx.Response(200, json={"code": 20000, "LimitNum": "5.00"}),
    ])
    async with httpx.AsyncClient() as http:
        data = await _make_client(http).call("/api/user/CurScore", {})
    assert data["LimitNum"] == "5.00"


@respx.mock
async def test_call_code_minus3_then_minus3_raises_auth_failed():
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/user/CurScore").mock(return_value=httpx.Response(
        200, json={"code": -3, "message": "token invalid"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _make_client(http).call("/api/user/CurScore", {})
    assert ei.value.reason == "gtreasure:auth_failed"


@respx.mock
async def test_call_52_treated_same_as_minus3():
    respx.post(f"{BASE}/api/user/login").mock(side_effect=[
        httpx.Response(200, json=_login_ok("Told")),
        httpx.Response(200, json=_login_ok("Tnew")),
    ])
    respx.post(f"{BASE}/api/user/CurScore").mock(side_effect=[
        httpx.Response(200, json={"code": 52, "message": "no permission"}),
        httpx.Response(200, json={"code": 20000, "LimitNum": "1.00"}),
    ])
    async with httpx.AsyncClient() as http:
        data = await _make_client(http).call("/api/user/CurScore", {})
    assert data["LimitNum"] == "1.00"


# ---- call() error classification ----

@respx.mock
async def test_call_code_21_is_terminal_operation_refused():
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/account/enterScore").mock(return_value=httpx.Response(
        200, json={"code": 21, "message": "充值失败：服务器维护中"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _make_client(http).call("/api/account/enterScore", {"score": "1"})
    assert ei.value.reason == "gtreasure:operation_refused"
    assert not isinstance(ei.value, TransientBackendError)


@respx.mock
async def test_call_code_167_is_transient_rate_limited(fake_redis):
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/account/enterScore").mock(return_value=httpx.Response(
        200, json={"code": 167, "message": "high frequency request"}))
    async with httpx.AsyncClient() as http:
        client = _make_client(http, fake_redis=fake_redis)
        with pytest.raises(TransientBackendError) as ei:
            await client.call("/api/account/enterScore", {"score": "1"}, throttle=True)
    assert ei.value.reason == "gtreasure:rate_limited"


@respx.mock
async def test_call_5xx_is_transient():
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/user/CurScore").mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _make_client(http).call("/api/user/CurScore", {})


@respx.mock
async def test_call_transport_error_is_transient():
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/user/CurScore").mock(side_effect=httpx.ConnectTimeout("boom"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _make_client(http).call("/api/user/CurScore", {})


# ---- throttle ----

@respx.mock
async def test_throttle_acquires_setnx_key_for_mutating_op(fake_redis):
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/account/enterScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "message": "ok"}))
    async with httpx.AsyncClient() as http:
        client = _make_client(http, fake_redis=fake_redis)
        await client.call("/api/account/enterScore", {"score": "1"}, throttle=True)
    # SET NX with ex=5 means the key exists with TTL > 0 immediately after the call.
    assert await fake_redis.exists("gtreasure_throttle:13") == 1
    ttl = await fake_redis.ttl("gtreasure_throttle:13")
    assert 0 < ttl <= 5


@respx.mock
async def test_non_mutating_call_does_not_touch_throttle_key(fake_redis):
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/user/CurScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "LimitNum": "5.00"}))
    async with httpx.AsyncClient() as http:
        client = _make_client(http, fake_redis=fake_redis)
        await client.call("/api/user/CurScore", {})        # NO throttle=True
    assert await fake_redis.exists("gtreasure_throttle:13") == 0


@respx.mock
async def test_throttle_serializes_concurrent_mutating_ops(fake_redis, monkeypatch):
    # Two ops on the same game must serialize: the second waits until the first's 5s lock expires.
    # Monkeypatch asyncio.sleep so the test runs fast but still exercises the SETNX poll loop.
    real_sleep = asyncio.sleep
    sleeps: list[float] = []

    async def fast_sleep(s):
        sleeps.append(s)
        # Burn the SETNX TTL down so the poll eventually acquires.
        await real_sleep(0)
        await fake_redis.delete("gtreasure_throttle:13")     # simulate TTL expiry

    monkeypatch.setattr("app.backends.goldentreasure.client.asyncio.sleep", fast_sleep)

    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/account/enterScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "message": "ok"}))

    async with httpx.AsyncClient() as http:
        client = _make_client(http, fake_redis=fake_redis)
        # Manually plant the throttle key as if a prior op held it.
        await fake_redis.set("gtreasure_throttle:13", b"1", nx=True, ex=5)
        await client.call("/api/account/enterScore", {"score": "1"}, throttle=True)

    assert sleeps, "_acquire_throttle should have polled at least once"
