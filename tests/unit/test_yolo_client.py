# tests/unit/test_yolo_client.py
import httpx
import pytest
import respx

from app.backends.base import BackendError
from app.backends.diagnostics import DiagnosticsRecorder
from app.backends.yolo.client import YoloClient
from app.backends.yolo.session import InMemorySessionStore

BASE = "https://yolo.test"
LOGIN_PAGE = '<form action="/admin/auth/login"><input name="password" type="password">' \
             '<script>Dcat.token = "TOK1";</script></form>'
ADMIN_PAGE = '<html>Dcat.token = "TOK1"; <span class="score">10.00</span></html>'


def _make_client(http, *, diagnostics=None, store=None):
    return YoloClient(
        base_url=BASE, username="webyolo1", password="Web@@1122",
        http_client=http, session_store=store or InMemorySessionStore(), game_id=1,
        session_ttl_seconds=1800, diagnostics=diagnostics,
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
async def test_double_auth_failure_raises_auth_failed():
    """419 on both the first and the retry write -> BackendError('yolo:auth_failed')."""
    respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, json={"status": True}))
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=ADMIN_PAGE))
    respx.post(f"{BASE}/admin/dcat-api/form").mock(
        return_value=httpx.Response(419, json={"message": "CSRF token mismatch"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError, match="auth_failed"):
            await _make_client(http).post_form("/admin/dcat-api/form", {"type": 1})


@respx.mock
async def test_get_text_double_auth_failure_raises_auth_failed():
    respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, json={"status": True}))
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=ADMIN_PAGE))
    respx.get(f"{BASE}/admin/refresh_score").mock(return_value=httpx.Response(419, text=""))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError, match="auth_failed"):
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


# ---- diagnostics: login sub-steps + session events ----

@respx.mock
async def test_fresh_login_records_three_login_substeps_and_emits_fresh():
    respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, json={"status": True}))
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=ADMIN_PAGE))
    respx.post(f"{BASE}/admin/dcat-api/form").mock(
        return_value=httpx.Response(200, json={"status": True, "data": {}}))
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient() as http:
        await _make_client(http, diagnostics=rec).post_form("/admin/dcat-api/form", {"type": 1})
    snap = rec.snapshot()
    steps = {s["name"]: s for s in snap["steps"]}
    assert "login.page" in steps and steps["login.page"]["phase"] == "auth"
    assert "login.submit" in steps and steps["login.submit"]["phase"] == "auth"
    assert "login.confirm" in steps and steps["login.confirm"]["phase"] == "auth"
    assert snap["session_reuse"] == "fresh"


@respx.mock
async def test_cached_session_get_session_emits_hit():
    login = respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, json={"status": True}))
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=ADMIN_PAGE))
    respx.post(f"{BASE}/admin/dcat-api/form").mock(
        return_value=httpx.Response(200, json={"status": True, "data": {}}))
    store = InMemorySessionStore()
    async with httpx.AsyncClient() as http:
        client = _make_client(http, store=store)
        await client.post_form("/admin/dcat-api/form", {"type": 1})       # fresh login, populates store
        rec = DiagnosticsRecorder()
        cached = _make_client(http, diagnostics=rec, store=store)
        await cached.post_form("/admin/dcat-api/form", {"type": 2})       # should hit the cached session
    assert login.call_count == 1
    assert rec.snapshot()["session_reuse"] == "hit"


@respx.mock
async def test_auth_failure_retry_records_recovery_step_and_emits_relogin():
    respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, json={"status": True}))
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=ADMIN_PAGE))
    respx.post(f"{BASE}/admin/dcat-api/form").mock(side_effect=[
        httpx.Response(419, json={"message": "CSRF token mismatch"}),
        httpx.Response(200, json={"status": True, "data": {"message": "success"}}),
    ])
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient() as http:
        data = await _make_client(http, diagnostics=rec).post_form(
            "/admin/dcat-api/form", {"type": 1}, step="recharge.post", phase="primary")
    assert data == {"message": "success"}
    snap = rec.snapshot()
    assert snap["session_reuse"] == "relogin"
    names = [s["name"] for s in snap["steps"]]
    assert "recovery" in names
    assert "recharge.post" in names


@respx.mock
async def test_post_form_records_the_given_step_name():
    respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, json={"status": True}))
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=ADMIN_PAGE))
    respx.post(f"{BASE}/admin/dcat-api/form").mock(
        return_value=httpx.Response(200, json={"status": True, "data": {}}))
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient() as http:
        await _make_client(http, diagnostics=rec).post_form(
            "/admin/dcat-api/form", {"type": 1}, step="reset.post", phase="primary")
    steps = {s["name"]: s for s in rec.snapshot()["steps"]}
    assert "reset.post" in steps
    assert steps["reset.post"]["phase"] == "primary"


@respx.mock
async def test_get_text_records_the_given_step_name():
    respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, json={"status": True}))
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=ADMIN_PAGE))
    respx.get(f"{BASE}/admin/refresh_score").mock(return_value=httpx.Response(200, text="10.00"))
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient() as http:
        await _make_client(http, diagnostics=rec).get_text(
            "/admin/refresh_score", step="balance.read", phase="primary")
    steps = {s["name"]: s for s in rec.snapshot()["steps"]}
    assert "balance.read" in steps
    assert steps["balance.read"]["phase"] == "primary"


@respx.mock
async def test_get_text_auth_failure_retry_records_recovery_step_and_emits_relogin():
    respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, json={"status": True}))
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=ADMIN_PAGE))
    respx.get(f"{BASE}/admin/refresh_score").mock(side_effect=[
        httpx.Response(419, text=""),
        httpx.Response(200, text="10.00"),
    ])
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient() as http:
        text = await _make_client(http, diagnostics=rec).get_text(
            "/admin/refresh_score", step="agent_balance.read", phase="primary")
    assert text.strip() == "10.00"
    snap = rec.snapshot()
    assert snap["session_reuse"] == "relogin"
    names = [s["name"] for s in snap["steps"]]
    assert "recovery" in names
    assert "agent_balance.read" in names
