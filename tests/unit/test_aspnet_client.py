import time

import httpx
import pytest
import respx

from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.session import CachedSession, InMemoryCookieSessionStore
from tests.conftest import FakeCaptchaSolver

BASE = "https://os.test"

_LOGIN_PAGE = """
<form><input type="hidden" name="__VIEWSTATE" value="VS_A" />
<input type="hidden" name="__VIEWSTATEGENERATOR" value="CA0B0334" />
<input type="hidden" name="__EVENTVALIDATION" value="EV_A" />
<img src="Tools/VerifyImagePage.aspx?1" /></form>
"""


def _mock_login_chain(cookie_value: str = "COOKIE_NEW"):
    """Stand up the GET-default + GET-captcha + POST-default chain returning success."""
    respx.get(f"{BASE}/default.aspx").mock(
        return_value=httpx.Response(
            200, text=_LOGIN_PAGE,
            headers={"Set-Cookie": f"ASP.NET_SessionId={cookie_value}; path=/"},
        )
    )
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?1").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8FAKE")
    )
    respx.post(f"{BASE}/default.aspx").mock(
        return_value=httpx.Response(301, text="", headers={"Location": "Cashier.aspx"})
    )


def _client(http, store=None, captcha=None) -> AspnetCashierClient:
    return AspnetCashierClient(
        base_url=BASE, username="u", password="p",
        http_client=http,
        session_store=store or InMemoryCookieSessionStore(),
        captcha_solver=captcha or FakeCaptchaSolver(),
        game_id=42,
        session_ttl_seconds=1800,
        lock_ttl_seconds=20,
        lock_acquire_timeout_seconds=5.0,
        captcha_login_max_attempts=3,
        driver_prefix="orionstars",
    )


@respx.mock
async def test_get_or_login_returns_cached_cookie_when_fresh():
    store = InMemoryCookieSessionStore()
    await store.set(
        42, CachedSession(cookie="CACHED", expires_at=int(time.time()) + 3600),
        ttl_seconds=3600,
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        cookie = await c.get_or_login()
    assert cookie == "CACHED"
    # No HTTP calls were made
    assert len(respx.calls) == 0


@respx.mock
async def test_get_or_login_performs_login_on_cache_miss():
    _mock_login_chain("NEW_COOKIE")
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http)
        cookie = await c.get_or_login()
    assert cookie == "NEW_COOKIE"


@respx.mock
async def test_get_or_login_treats_expired_cache_as_miss():
    store = InMemoryCookieSessionStore()
    await store.set(
        42, CachedSession(cookie="EXPIRED", expires_at=int(time.time()) - 60),
        ttl_seconds=60,
    )
    _mock_login_chain("REFRESHED")
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        cookie = await c.get_or_login()
    assert cookie == "REFRESHED"


# Need TransientBackendError for the session-death test
from app.backends.base import TransientBackendError  # noqa: E402 - import grouped with the new tests

# --- request() ---

_NRE_500 = """
<html><body>Server Error in '/' Application.
<br>System.NullReferenceException: Object reference not set...</body></html>
"""


@respx.mock
async def test_request_attaches_session_cookie_and_accept_language():
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="USE_ME", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    route = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text="0.00@0.00|<html/>")
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        body = await c.request_text("POST", "/Module/AccountManager/AccountsList.aspx",
                                    form={"getscoreuserid": "1"})
    assert body.startswith("0.00@0.00|")
    sent = route.calls.last.request
    assert sent.headers.get("accept-language", "").startswith("en")
    cookie_hdr = sent.headers.get("cookie", "")
    assert "ASP.NET_SessionId=USE_ME" in cookie_hdr


@respx.mock
async def test_request_retries_once_after_session_death_500_nre():
    """First call returns the NRE 500 (dead session). Client clears cache, re-logs in, retries."""
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="DEAD", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    _mock_login_chain("REVIVED")
    route = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        side_effect=[
            httpx.Response(500, text=_NRE_500),
            httpx.Response(200, text="9.99@0.00|<html/>"),
        ]
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        body = await c.request_text("POST", "/Module/AccountManager/AccountsList.aspx",
                                    form={"getscoreuserid": "1"})
    assert body.startswith("9.99@0.00|")
    assert route.call_count == 2
    second = route.calls[-1].request
    assert "ASP.NET_SessionId=REVIVED" in second.headers.get("cookie", "")


@respx.mock
async def test_request_does_not_retry_more_than_once_on_repeated_500():
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="DEAD", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    _mock_login_chain("REVIVED")
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(500, text=_NRE_500),
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        with pytest.raises(TransientBackendError):
            await c.request_text("POST", "/Module/AccountManager/AccountsList.aspx",
                                 form={"getscoreuserid": "1"})
