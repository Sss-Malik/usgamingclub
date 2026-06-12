import json
import time

import httpx
import pytest
import respx

from app.backends.base import BackendError, TransientBackendError
from app.backends.ultrapanda.client import FINGERPRINT, UltraPandaClient
from app.backends.ultrapanda.crypto import decrypt_xtoken
from app.backends.ultrapanda.session import (
    CachedSession,
    InMemoryTokenStore,
)

BASE = "https://up.test"


def _client(http, store=None, redis=None) -> UltraPandaClient:
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
