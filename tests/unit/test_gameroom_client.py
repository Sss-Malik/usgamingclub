# tests/unit/test_gameroom_client.py
import asyncio
import time

import httpx
import pytest
import respx

from app.backends.base import BackendError, TransientBackendError
from app.backends.gameroom.client import GameroomClient
from app.backends.gameroom.session import CachedSession, InMemorySessionStore

BASE = "https://gr.test"


def _client(http, store=None):
    return GameroomClient(
        base_url=BASE, username="u", password="p",
        http_client=http, session_store=store or InMemorySessionStore(), game_id=11,
    )


def _login_ok(token="T1", expires_in=21600):
    return {"status_code": 200, "message": "ok", "token": token,
            "expires_time": int(time.time()) + expires_in, "money": "5.00"}


# --- login ---

@respx.mock
async def test_login_posts_form_urlencoded_without_bearer_and_returns_token():
    route = respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("Tabc")))
    async with httpx.AsyncClient() as http:
        token = await _client(http).get_token()
    assert token == "Tabc"
    sent = route.calls.last.request
    body = sent.content.decode()
    assert "username=u" in body and "password=p" in body
    assert "captcha" not in body                                   # captcha intentionally omitted
    assert sent.headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert "authorization" not in [h.lower() for h in sent.headers.keys()]


@respx.mock
async def test_login_430_is_terminal_auth_failed():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(
        200, json={"status_code": 430, "message": "Username or password error"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _client(http).get_token()
    assert ei.value.reason == "gameroom:auth_failed"
    assert not isinstance(ei.value, TransientBackendError)


@respx.mock
async def test_login_500_is_transient():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _client(http).get_token()


# --- get_token reuse + invalidate ---

@respx.mock
async def test_get_token_returns_cached_when_present_and_fresh():
    route = respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("Tx")))
    store = InMemorySessionStore()
    await store.set(11, CachedSession(token="cached", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    async with httpx.AsyncClient() as http:
        token = await _client(http, store=store).get_token()
    assert token == "cached"
    assert route.call_count == 0                                   # NO login happened


@respx.mock
async def test_get_token_with_invalidate_skips_login_if_cache_already_holds_newer():
    # Double-checked-locking regression: another worker already refreshed; we must not re-login.
    route = respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("would_be_new")))
    store = InMemorySessionStore()
    await store.set(11, CachedSession(token="T2_already_fresh", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    async with httpx.AsyncClient() as http:
        token = await _client(http, store=store).get_token(invalidate="T1_dead")
    assert token == "T2_already_fresh"
    assert route.call_count == 0


@respx.mock
async def test_get_token_with_invalidate_logs_in_when_cache_still_holds_dead_token():
    route = respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("Tnew")))
    store = InMemorySessionStore()
    await store.set(11, CachedSession(token="T1_dead", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    async with httpx.AsyncClient() as http:
        token = await _client(http, store=store).get_token(invalidate="T1_dead")
    assert token == "Tnew"
    assert route.call_count == 1


@respx.mock
async def test_concurrent_get_token_under_lock_logs_in_only_once():
    """Two workers see empty cache simultaneously. The login lock must serialize so only ONE
    /api/login is issued; the second worker reads the freshly-cached token after the lock releases."""
    route = respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("Tonce")))
    store = InMemorySessionStore()
    async with httpx.AsyncClient() as http:
        c1 = _client(http, store=store)
        c2 = _client(http, store=store)
        tok1, tok2 = await asyncio.gather(c1.get_token(), c2.get_token())
    assert tok1 == tok2 == "Tonce"
    assert route.call_count == 1                                   # crucial: only one login


# --- call() with re-login-on-410 ---

@respx.mock
async def test_call_success_returns_data():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("T1")))
    respx.post(f"{BASE}/api/agent/getMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "data": {"money": "5.00"}}))
    async with httpx.AsyncClient() as http:
        data = await _client(http).call("POST", "/api/agent/getMoney")
    assert data == {"money": "5.00"}


@respx.mock
async def test_call_410_relogins_and_retries_once_successfully():
    respx.post(f"{BASE}/api/login").mock(side_effect=[
        httpx.Response(200, json=_login_ok("Told")),               # initial login
        httpx.Response(200, json=_login_ok("Tnew")),               # re-login after 410
    ])
    respx.post(f"{BASE}/api/agent/getMoney").mock(side_effect=[
        httpx.Response(200, json={"status_code": 410, "message": "Please login again"}),
        httpx.Response(200, json={"status_code": 200, "message": "ok", "data": {"money": "5.00"}}),
    ])
    async with httpx.AsyncClient() as http:
        data = await _client(http).call("POST", "/api/agent/getMoney")
    assert data == {"money": "5.00"}


@respx.mock
async def test_call_410_after_relogin_raises_auth_failed():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/agent/getMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 410, "message": "Please login again"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _client(http).call("POST", "/api/agent/getMoney")
    assert ei.value.reason == "gameroom:auth_failed"
    assert not isinstance(ei.value, TransientBackendError)


@respx.mock
async def test_call_500_is_transient():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/agent/getMoney").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _client(http).call("POST", "/api/agent/getMoney")


@respx.mock
async def test_call_business_400_is_terminal_mapped():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/player/playerInsert").mock(return_value=httpx.Response(
        200, json={"status_code": 400, "message": "Username already exists"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _client(http).call("POST", "/api/player/playerInsert", fields={"username": "x"})
    assert ei.value.reason == "gameroom:account_exists"
    assert not isinstance(ei.value, TransientBackendError)


@respx.mock
async def test_call_get_uses_query_params_and_bearer_header():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("Tjwt")))
    route = respx.get(f"{BASE}/api/player/agentMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "data": {"balance": 60}}))
    async with httpx.AsyncClient() as http:
        await _client(http).call("GET", "/api/player/agentMoney", params={"id": 123})
    sent = route.calls.last.request
    assert dict(sent.url.params) == {"id": "123"}
    assert sent.headers["authorization"] == "Bearer Tjwt"


@respx.mock
async def test_call_transport_error_is_transient():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/agent/getMoney").mock(side_effect=httpx.ConnectTimeout("boom"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _client(http).call("POST", "/api/agent/getMoney")


# --- call_raw ---

@respx.mock
async def test_call_raw_success_returns_full_envelope_with_list_data():
    # call_raw exists for endpoints whose `data` is a list (e.g. userList); call() unwraps dict data
    # only, so list-data callers must use call_raw to access the rows.
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.get(f"{BASE}/api/player/userList").mock(return_value=httpx.Response(
        200, json={"code": 0, "status_code": 200, "message": "Query successful",
                   "count": 1, "data": [{"id": 1, "Account": "x"}]}))
    async with httpx.AsyncClient() as http:
        envelope = await _client(http).call_raw("GET", "/api/player/userList", params={"account": "x"})
    assert envelope["status_code"] == 200
    assert envelope["data"] == [{"id": 1, "Account": "x"}]


@respx.mock
async def test_call_raw_transient_500_raises_not_returns():
    # Regression for the call_raw misclassification bug: a transient server error from a userList
    # lookup must surface as TransientBackendError (not silently passed back as an envelope so the
    # caller misclassifies it as terminal 'player_not_found').
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.get(f"{BASE}/api/player/userList").mock(return_value=httpx.Response(
        200, json={"status_code": 500, "message": "Service exception"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _client(http).call_raw("GET", "/api/player/userList", params={"account": "x"})


@respx.mock
async def test_call_raw_business_400_raises_terminal():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.get(f"{BASE}/api/player/userList").mock(return_value=httpx.Response(
        200, json={"status_code": 400, "message": "Operation failed"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _client(http).call_raw("GET", "/api/player/userList", params={"account": "x"})
    assert ei.value.reason == "gameroom:operation_failed"
    assert not isinstance(ei.value, TransientBackendError)


@respx.mock
async def test_call_raw_http_5xx_is_transient():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.get(f"{BASE}/api/player/userList").mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _client(http).call_raw("GET", "/api/player/userList", params={"account": "x"})
