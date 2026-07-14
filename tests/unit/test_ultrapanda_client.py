import json
import time

import httpx
import pytest
import respx

from app.backends.base import BackendError, TransientBackendError
from app.backends.diagnostics import DiagnosticsRecorder
from app.backends.ultrapanda.client import FINGERPRINT, UltraPandaClient
from app.backends.ultrapanda.crypto import decrypt_xtoken
from app.backends.ultrapanda.session import (
    CachedSession,
    InMemoryTokenStore,
)

BASE = "https://up.test"


def _client(http, store=None, redis=None, diagnostics=None) -> UltraPandaClient:
    return UltraPandaClient(
        base_url=BASE,
        username="TestUP159",
        password="Test1234",
        http_client=http,
        session_store=store or InMemoryTokenStore(),
        redis=redis,
        game_id=42,
        session_ttl_seconds=1800,
        throttle_ttl_seconds=6,
        throttle_acquire_timeout_seconds=2.0,
        session_lock_ttl_seconds=10,
        session_lock_acquire_timeout_seconds=2.0,
        driver_prefix="ultrapanda",
        diagnostics=diagnostics,
    )


# --- login ---

@respx.mock
async def test_login_posts_aes_encrypted_creds_and_caches_token(fake_redis):
    """The login body must carry AES-encrypted username/password, stime, auth_code='',
    and a valid `sign`. On success (code 20000), the returned token is cached verbatim."""
    captured: dict = {}

    def login_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.update(body)
        return httpx.Response(200, json={
            "code": 20000,
            "name": "TestUP159",
            "token": "Ul%2Ba9iVUvWnqlti2VP%2BatFnckAzxSNbIcEVrTxn%2F%2FTg%3D",
            "data": {},
        })

    respx.post(f"{BASE}/user/login").mock(side_effect=login_handler)
    store = InMemoryTokenStore()
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis)
        token = await c.get_or_login()

    assert token == "Ul%2Ba9iVUvWnqlti2VP%2BatFnckAzxSNbIcEVrTxn%2F%2FTg%3D"
    assert set(captured.keys()) >= {"username", "password", "stime", "auth_code", "sign"}
    assert captured["username"] != "TestUP159"
    assert captured["password"] != "Test1234"
    assert captured["auth_code"] == ""
    cached = await store.get(42)
    assert cached is not None
    assert cached.token == "Ul%2Ba9iVUvWnqlti2VP%2BatFnckAzxSNbIcEVrTxn%2F%2FTg%3D"


@respx.mock
async def test_login_bad_credentials_raises_terminal_backend_error(fake_redis):
    respx.post(f"{BASE}/user/login").mock(
        return_value=httpx.Response(200, json={"code": 5, "message": "帐号或密码错误"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, redis=fake_redis)
        with pytest.raises(BackendError) as ei:
            await c.get_or_login()
    assert ei.value.reason == "ultrapanda:bad_credentials"
    assert not isinstance(ei.value, TransientBackendError)


@respx.mock
async def test_login_5xx_is_transient(fake_redis):
    respx.post(f"{BASE}/user/login").mock(return_value=httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, redis=fake_redis)
        with pytest.raises(TransientBackendError):
            await c.get_or_login()


@respx.mock
async def test_get_or_login_returns_cached_token_when_fresh(fake_redis):
    store = InMemoryTokenStore()
    await store.set(42, CachedSession(token="cached_tok", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis)
        token = await c.get_or_login()
    assert token == "cached_tok"
    assert len(respx.calls) == 0


# --- signed call ---

@respx.mock
async def test_signed_call_injects_stime_sign_and_headers(fake_redis):
    """Every non-login POST gets `sign` + `stime` in the body and `x-time`, `x-token`,
    `x-fingerprint` headers."""
    store = InMemoryTokenStore()
    await store.set(42, CachedSession(token="testtok", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    route = respx.post(f"{BASE}/user/CurScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "LimitNum": "3.00"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis)
        body = await c.call("/user/CurScore", {"token": "testtok"})
    assert body == {"code": 20000, "LimitNum": "3.00"}
    sent = route.calls.last.request
    sent_body = json.loads(sent.content)
    assert "stime" in sent_body and isinstance(sent_body["stime"], int)
    assert "sign" in sent_body and len(sent_body["sign"]) == 32
    ms_time = int(sent.headers["x-time"])
    assert len(sent.headers["x-time"]) == 13
    assert sent.headers["x-fingerprint"] == FINGERPRINT
    assert decrypt_xtoken(sent.headers["x-token"], ms_time) == "testtok"
    assert sent.headers["content-type"] == "application/json;charset=UTF-8"


# --- throttle ---

@respx.mock
async def test_throttle_blocks_second_enter_score_within_ttl(fake_redis):
    """SET NX vpower_throttle:{game_id} ex=6 — second enterScore inside TTL must wait."""
    store = InMemoryTokenStore()
    await store.set(42, CachedSession(token="t", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "ok"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis)
        await fake_redis.set("vpower_throttle:42", b"1", ex=6, nx=True)
        with pytest.raises(TransientBackendError, match="throttle_acquire_timeout"):
            await c.call_throttled("/account/enterScore", {"account": "x", "score": "1", "user_type": 0})


@respx.mock
async def test_throttle_allows_call_when_key_absent(fake_redis):
    store = InMemoryTokenStore()
    await store.set(42, CachedSession(token="t", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "进分成功"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis)
        body = await c.call_throttled("/account/enterScore",
                                     {"account": "x", "score": "1", "user_type": 0})
    assert body["code"] == 20000
    assert await fake_redis.exists("vpower_throttle:42") == 1


@respx.mock
async def test_throttle_rate_limit_code_167_is_transient(fake_redis):
    store = InMemoryTokenStore()
    await store.set(42, CachedSession(token="t", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 167, "message": "high frequency request"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis)
        with pytest.raises(TransientBackendError, match="rate_limited"):
            await c.call_throttled("/account/enterScore",
                                  {"account": "x", "score": "1", "user_type": 0},
                                  op="recharge")


# --- session-death detection + retry-once-after-relogin ---

@respx.mock
async def test_call_retries_once_after_session_expired_1086(fake_redis):
    """Code 1086 (Not logged in) triggers cache-clear → re-login → retry the original call."""
    store = InMemoryTokenStore()
    await store.set(42, CachedSession(token="DEAD", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    respx.post(f"{BASE}/user/login").mock(
        return_value=httpx.Response(200, json={"code": 20000, "token": "FRESH"})
    )
    route = respx.post(f"{BASE}/user/CurScore").mock(
        side_effect=[
            httpx.Response(200, json={"code": 1086, "message": "Not logged in"}),
            httpx.Response(200, json={"code": 20000, "LimitNum": "1.50"}),
        ]
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis)
        body = await c.call("/user/CurScore", {"token": "DEAD"})
    assert body == {"code": 20000, "LimitNum": "1.50"}
    assert route.call_count == 2
    cached = await store.get(42)
    assert cached is not None and cached.token == "FRESH"


@respx.mock
async def test_call_does_not_retry_more_than_once_on_repeated_1086(fake_redis):
    store = InMemoryTokenStore()
    await store.set(42, CachedSession(token="DEAD", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    respx.post(f"{BASE}/user/login").mock(
        return_value=httpx.Response(200, json={"code": 20000, "token": "FRESH"})
    )
    respx.post(f"{BASE}/user/CurScore").mock(
        return_value=httpx.Response(200, json={"code": 1086, "message": "Not logged in"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis)
        with pytest.raises(TransientBackendError, match="session_dead_after_relogin"):
            await c.call("/user/CurScore", {"token": "DEAD"})


# --- diagnostics: provider_code on the login-time map_code raise (no provider_message) ---

@respx.mock
async def test_login_business_error_carries_provider_code_terminal(fake_redis):
    respx.post(f"{BASE}/user/login").mock(
        return_value=httpx.Response(200, json={"code": 5, "message": "帐号或密码错误"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, redis=fake_redis)
        with pytest.raises(BackendError) as ei:
            await c.get_or_login()
    assert ei.value.reason == "ultrapanda:bad_credentials"
    assert ei.value.provider_code == 5
    assert ei.value.provider_message is None          # vpower login errors carry no message field


@respx.mock
async def test_login_business_error_carries_provider_code_transient(fake_redis):
    respx.post(f"{BASE}/user/login").mock(
        return_value=httpx.Response(200, json={"code": 167, "message": "high frequency request"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, redis=fake_redis)
        with pytest.raises(TransientBackendError) as ei:
            await c.get_or_login()
    assert ei.value.reason == "ultrapanda:rate_limited"
    assert ei.value.provider_code == 167
    assert ei.value.provider_message is None


# --- diagnostics: session events from get_or_login ---

@respx.mock
async def test_get_or_login_cache_hit_emits_session_hit(fake_redis):
    store = InMemoryTokenStore()
    await store.set(42, CachedSession(token="cached_tok", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis, diagnostics=rec)
        token = await c.get_or_login()
    assert token == "cached_tok"
    assert len(respx.calls) == 0
    assert rec.snapshot()["session_reuse"] == "hit"


@respx.mock
async def test_get_or_login_fresh_login_emits_session_fresh_and_login_submit_step(fake_redis):
    respx.post(f"{BASE}/user/login").mock(
        return_value=httpx.Response(200, json={"code": 20000, "token": "FRESH"})
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, redis=fake_redis, diagnostics=rec)
        token = await c.get_or_login()
    assert token == "FRESH"
    snap = rec.snapshot()
    assert snap["session_reuse"] == "fresh"
    steps = {s["name"]: s for s in snap["steps"]}
    assert "login.submit" in steps
    assert steps["login.submit"]["phase"] == "auth"
    assert steps["login.submit"]["ok"] is True


# --- diagnostics: throttle.acquire + named primary step ---

@respx.mock
async def test_call_throttled_records_throttle_acquire_step_as_non_http(fake_redis):
    store = InMemoryTokenStore()
    await store.set(42, CachedSession(token="t", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "ok"})
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis, diagnostics=rec)
        await c.call_throttled("/account/enterScore",
                               {"account": "x", "score": "1", "user_type": 0},
                               step="recharge.post")
    steps = {s["name"]: s for s in rec.snapshot()["steps"]}
    assert "throttle.acquire" in steps
    assert steps["throttle.acquire"]["phase"] == "preflight"
    assert steps["throttle.acquire"]["http"] is False
    assert "recharge.post" in steps


@respx.mock
async def test_call_records_the_given_step_name(fake_redis):
    store = InMemoryTokenStore()
    await store.set(42, CachedSession(token="t", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    respx.post(f"{BASE}/user/CurScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "LimitNum": "3.00"})
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis, diagnostics=rec)
        await c.call("/user/CurScore", {"token": "t"}, step="agent_balance.read")
    steps = {s["name"]: s for s in rec.snapshot()["steps"]}
    assert "agent_balance.read" in steps
    assert steps["agent_balance.read"]["phase"] == "primary"
    assert steps["agent_balance.read"]["http"] is True


# --- diagnostics: 1086 recovery emits relogin + recovery.relogin step ---

@respx.mock
async def test_call_1086_recovery_emits_relogin_session_event_and_recovery_step(fake_redis):
    store = InMemoryTokenStore()
    await store.set(42, CachedSession(token="DEAD", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    respx.post(f"{BASE}/user/login").mock(
        return_value=httpx.Response(200, json={"code": 20000, "token": "FRESH"})
    )
    respx.post(f"{BASE}/user/CurScore").mock(
        side_effect=[
            httpx.Response(200, json={"code": 1086, "message": "Not logged in"}),
            httpx.Response(200, json={"code": 20000, "LimitNum": "1.50"}),
        ]
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis, diagnostics=rec)
        body = await c.call("/user/CurScore", {"token": "DEAD"}, step="balance.read")
    assert body == {"code": 20000, "LimitNum": "1.50"}
    snap = rec.snapshot()
    assert snap["session_reuse"] == "relogin"
    names = [s["name"] for s in snap["steps"]]
    assert "recovery.relogin" in names
    assert "balance.read" in names
