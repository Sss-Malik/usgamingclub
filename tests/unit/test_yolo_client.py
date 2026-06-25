# tests/unit/test_yolo_client.py
import httpx
import pytest
import respx

from app.backends.base import BackendError
from app.backends.yolo.client import YoloClient
from app.backends.yolo.session import InMemorySessionStore

BASE = "https://yolo.test"
LOGIN_PAGE = '<form action="/admin/auth/login"><input name="password" type="password">' \
             '<script>Dcat.token = "TOK1";</script></form>'
ADMIN_PAGE = '<html>Dcat.token = "TOK1"; <span class="score">10.00</span></html>'


def _make_client(http):
    return YoloClient(
        base_url=BASE, username="webyolo1", password="Web@@1122",
        http_client=http, session_store=InMemorySessionStore(), game_id=1,
        session_ttl_seconds=1800,
    )


@respx.mock
async def test_login_then_post_form_success():
    respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(
        return_value=httpx.Response(200, json={"status": True},
                                    headers={"Set-Cookie": "laravel_session=SESS1; path=/"}))
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=ADMIN_PAGE))
    form = respx.post(f"{BASE}/admin/dcat-api/form").mock(
        return_value=httpx.Response(200, json={"status": True, "data": {"message": "success"}}))

    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        data = await client.post_form("/admin/dcat-api/form", {"type": 1, "input_score": 5})
    assert data == {"message": "success"}
    body = form.calls.last.request.content.decode()
    assert "_token=TOK1" in body and "input_score=5" in body
    assert form.calls.last.request.headers["X-CSRF-TOKEN"] == "TOK1"
    assert form.calls.last.request.headers["X-Requested-With"] == "XMLHttpRequest"


@respx.mock
async def test_business_error_raises_terminal():
    respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, json={"status": True}))
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=ADMIN_PAGE))
    respx.post(f"{BASE}/admin/dcat-api/form").mock(
        return_value=httpx.Response(200, json={"status": False, "data": {"message": "The score is insufficient"}}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError, match="insufficient_balance"):
            await _make_client(http).post_form("/admin/dcat-api/form", {"type": 1})


@respx.mock
async def test_session_is_cached_login_happens_once():
    login = respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, json={"status": True}))
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=ADMIN_PAGE))
    respx.post(f"{BASE}/admin/dcat-api/form").mock(
        return_value=httpx.Response(200, json={"status": True, "data": {}}))
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        await client.post_form("/admin/dcat-api/form", {"type": 1})
        await client.post_form("/admin/dcat-api/form", {"type": 2})
    assert login.call_count == 1  # second call reused the cached session


@respx.mock
async def test_auth_failure_triggers_relogin_and_retry():
    respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, json={"status": True}))
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=ADMIN_PAGE))
    # First write 419 (CSRF expired), second write succeeds.
    respx.post(f"{BASE}/admin/dcat-api/form").mock(side_effect=[
        httpx.Response(419, json={"message": "CSRF token mismatch"}),
        httpx.Response(200, json={"status": True, "data": {"message": "success"}}),
    ])
    async with httpx.AsyncClient() as http:
        data = await _make_client(http).post_form("/admin/dcat-api/form", {"type": 1})
    assert data == {"message": "success"}


@respx.mock
async def test_login_failure_when_creds_rejected():
    respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, json={"status": True}))
    # player_list still shows the login page => not authenticated.
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError, match="login_failed"):
            await _make_client(http).get_text("/admin/refresh_score")


@respx.mock
async def test_get_text_returns_body():
    respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, json={"status": True}))
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=ADMIN_PAGE))
    respx.get(f"{BASE}/admin/refresh_score").mock(return_value=httpx.Response(200, text="10.00"))
    async with httpx.AsyncClient() as http:
        text = await _make_client(http).get_text("/admin/refresh_score")
    assert text.strip() == "10.00"
