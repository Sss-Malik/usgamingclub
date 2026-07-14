# tests/unit/test_gameroom_backend.py
import time

import httpx
import pytest
import respx

from app.backends.base import BackendError
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.diagnostics import DiagnosticsRecorder
from app.backends.gameroom.backend import GameroomBackend
from app.backends.gameroom.client import GameroomClient
from app.backends.gameroom.session import InMemorySessionStore

BASE = "https://gr.test"


def _creds():
    return GameCredentials(
        game_id=11, name="Gameroom",
        backend_url=BASE, login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="gameroom",
    )


def _ctx(*, account=True, external="2998032", username="apifull9983654",
         idem="idem-1", account_username=None, user_id=51, diagnostics=None):
    acct = AccountIdentity(3001, user_id, 11, username, external) if account else None
    return BackendContext(credentials=_creds(), user_id=user_id, account=acct,
                          idempotency_key=idem, account_username=account_username,
                          diagnostics=diagnostics)


def _backend(http, diagnostics=None):
    client = GameroomClient(
        base_url=BASE, username="u", password="p",
        http_client=http, session_store=InMemorySessionStore(), game_id=11,
        diagnostics=diagnostics,
    )
    return GameroomBackend(client)


def _login_ok():
    return {"status_code": 200, "message": "ok", "token": "Tjwt",
            "expires_time": int(time.time()) + 3600, "money": "5.00"}


def _mock_login():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok()))


# ---- AGENT_BALANCE ----

@respx.mock
async def test_agent_balance_reads_data_money():
    _mock_login()
    respx.post(f"{BASE}/api/agent/getMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "data": {"money": "5.00"}}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http).agent_balance(_ctx(account=False))
    assert r.agent_balance == 5.0


@respx.mock
async def test_agent_balance_falls_back_to_top_level_money():
    _mock_login()
    respx.post(f"{BASE}/api/agent/getMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "money": "5.00"}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http).agent_balance(_ctx(account=False))
    assert r.agent_balance == 5.0


@respx.mock
async def test_agent_balance_missing_value_is_terminal():
    _mock_login()
    respx.post(f"{BASE}/api/agent/getMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http).agent_balance(_ctx(account=False))
    assert ei.value.reason == "gameroom:agent_balance_missing"


# ---- READ_BALANCE ----

@respx.mock
async def test_read_balance_uses_external_user_id_and_returns_dollars():
    _mock_login()
    route = respx.get(f"{BASE}/api/player/agentMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok",
                   "data": {"username": "apifull9983654", "balance": 0, "cusBlance": "4.00"}}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http).read_balance(_ctx())
    assert r.balance == 0.0
    assert dict(route.calls.last.request.url.params) == {"id": "2998032"}


# ---- player_id fallback ----

@respx.mock
async def test_player_id_falls_back_to_userList_exact_match():
    _mock_login()
    respx.get(f"{BASE}/api/player/userList").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "count": 2, "data": [
            {"Id": 1, "id": 1, "Account": "user_no_ext_typo", "score": 0},
            {"Id": 99, "id": 99, "Account": "user_no_ext", "score": 0},
        ]}))
    respx.get(f"{BASE}/api/player/agentMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "data": {"balance": 5}}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http).read_balance(_ctx(external=None, username="user_no_ext"))
    assert r.balance == 5.0


@respx.mock
async def test_player_id_no_exact_match_raises_player_not_found():
    _mock_login()
    respx.get(f"{BASE}/api/player/userList").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "count": 1,
                   "data": [{"Id": 99, "id": 99, "Account": "different_user", "score": 0}]}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http).read_balance(_ctx(external=None, username="user_no_ext"))
    assert ei.value.reason == "gameroom:player_not_found"


# ---- CREATE_ACCOUNT ----

@respx.mock
async def test_create_account_posts_username_and_password_returns_id():
    _mock_login()
    route = respx.post(f"{BASE}/api/player/playerInsert").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "Insert successful",
                   "data": {"id": 2998032, "account": "apifull9983654", "password": "Test1122", "balance": "0"}}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http).create_account(_ctx(account=False, account_username="apifull9983654"))
    assert r.username == "apifull9983654" and r.external_user_id == "2998032" and r.password.isalnum()
    body = route.calls.last.request.content.decode()
    assert "username=apifull9983654" in body
    assert "nickname=apifull9983654" in body
    assert "money=0" in body
    assert "password=" in body and "password_confirmation=" in body


@respx.mock
async def test_create_account_requires_account_username():
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http).create_account(_ctx(account=False, account_username=None))
    assert ei.value.reason == "account_username_required"


def _mock_agent_money(agent="10.00", player=5):
    """Mock the agentMoney pre-fetch that recharge/redeem call before mutating ops."""
    respx.get(f"{BASE}/api/player/agentMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok",
                   "data": {"username": "apifull9983654", "balance": player, "cusBlance": agent}}))


# ---- RECHARGE ----

@respx.mock
async def test_recharge_sends_integer_dollars_with_fresh_agent_snapshot():
    # Server rejects stale/empty `available_balance` with "Available balance has changed".
    # We pre-fetch via agentMoney and pass cusBlance verbatim.
    _mock_login()
    _mock_agent_money(agent="10.00")
    route = respx.post(f"{BASE}/api/player/agentRecharge").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "Recharge successful",
                   "data": {"balance": "1", "bonus": 0, "remark": "", "total_balance": "1.00"}}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http).recharge(_ctx(), amount=50)
    body = route.calls.last.request.content.decode()
    assert "id=2998032" in body
    assert "available_balance=10.00" in body                                        # fresh from agentMoney
    assert "opera_type=0" in body
    assert "bonus=0" in body
    assert "balance=50" in body                                                     # wire value "50"
    assert "remark=" in body and "remark=&" in (body + "&")                         # empty
    assert r.balance == 1.0                                                         # float("1.00")


# ---- REDEEM ----

@respx.mock
async def test_redeem_sends_fresh_player_snapshot_succeeds_with_no_data_block():
    _mock_login()
    _mock_agent_money(player=7)
    route = respx.post(f"{BASE}/api/player/agentWithdraw").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "Withdraw successful"}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http).redeem(_ctx(), amount=30)
    body = route.calls.last.request.content.decode()
    assert "id=2998032" in body
    assert "customer_balance=7" in body                                             # fresh from agentMoney
    assert "opera_type=1" in body
    assert "balance=30" in body                                                     # wire value "30"
    assert r.balance is None                                                        # response has no data


@respx.mock
async def test_redeem_insufficient_user_balance_is_terminal():
    _mock_login()
    _mock_agent_money()                                                             # pre-fetch happens before withdraw
    respx.post(f"{BASE}/api/player/agentWithdraw").mock(return_value=httpx.Response(
        200, json={"status_code": 400,
                   "message": "Withdrawal amount is greater than customer balance. Please check and withdraw again"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http).redeem(_ctx(), amount=1)
    assert ei.value.reason == "gameroom:insufficient_user_balance"


@respx.mock
async def test_recharge_available_balance_changed_is_terminal_business_error():
    # Even with fresh pre-fetch, a concurrent op could change the agent balance between
    # agentMoney and agentRecharge. Confirm the failure surfaces cleanly as terminal.
    _mock_login()
    _mock_agent_money(agent="10.00")
    respx.post(f"{BASE}/api/player/agentRecharge").mock(return_value=httpx.Response(
        200, json={"status_code": 400,
                   "message": "Available balance has changed. Please refresh and recharge again"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http).recharge(_ctx(), amount=1)
    assert "Available balance has changed" in ei.value.reason
    assert ei.value.reason.startswith("gameroom:business_error:")


# ---- RESET_PASSWORD ----

@respx.mock
async def test_reset_password_posts_complex_password_and_returns_it():
    import re
    _mock_login()
    route = respx.post(f"{BASE}/api/player/reset").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "Reset successful"}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http).reset_password(_ctx())
    assert re.fullmatch(r"[A-Z][a-z]+[!@#$%&*]\d{2}", r.password), r.password
    body = route.calls.last.request.content.decode()
    assert "id=2998032" in body
    assert "password=" in body and "password_confirmation=" in body


# ---- diagnostics: named steps + snapshot marks ----

@respx.mock
async def test_recharge_marks_balance_before_and_records_recharge_snapshot_step():
    _mock_login()
    _mock_agent_money(agent="10.00", player=5)
    respx.post(f"{BASE}/api/player/agentRecharge").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "Recharge successful",
                   "data": {"balance": "1", "bonus": 0, "remark": "", "total_balance": "1.00"}}))
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient() as http:
        r = await _backend(http, diagnostics=rec).recharge(_ctx(diagnostics=rec), amount=50)
    snap = rec.snapshot()
    assert snap["balance_before"] == 5.0                            # player balance from agentMoney's "balance"
    assert snap["balance_after"] == "1.00"                          # recharge only: data["total_balance"]
    names = [s["name"] for s in snap["steps"]]
    assert "recharge.snapshot" in names
    assert "recharge.post" in names
    assert r.balance == 1.0


@respx.mock
async def test_redeem_marks_balance_before_and_records_redeem_snapshot_step():
    _mock_login()
    _mock_agent_money(player=7)
    respx.post(f"{BASE}/api/player/agentWithdraw").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "Withdraw successful"}))
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient() as http:
        await _backend(http, diagnostics=rec).redeem(_ctx(diagnostics=rec), amount=30)
    snap = rec.snapshot()
    assert snap["balance_before"] == 7.0
    assert snap["balance_after"] is None                            # redeem does NOT mark balance_after
    names = [s["name"] for s in snap["steps"]]
    assert "redeem.snapshot" in names
    assert "redeem.post" in names


@respx.mock
async def test_create_account_marks_external_user_id_and_records_create_post_step():
    _mock_login()
    respx.post(f"{BASE}/api/player/playerInsert").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "Insert successful",
                   "data": {"id": 2998032, "account": "apifull9983654", "password": "Test1122", "balance": "0"}}))
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient() as http:
        r = await _backend(http, diagnostics=rec).create_account(
            _ctx(account=False, account_username="apifull9983654", diagnostics=rec))
    snap = rec.snapshot()
    assert snap["external_user_id"] == "2998032"
    assert "create.post" in [s["name"] for s in snap["steps"]]
    assert r.external_user_id == "2998032"


@respx.mock
async def test_read_balance_records_resolve_and_balance_read_steps_and_marks_cached_external_id():
    _mock_login()
    route = respx.get(f"{BASE}/api/player/agentMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok",
                   "data": {"username": "apifull9983654", "balance": 0, "cusBlance": "4.00"}}))
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient() as http:
        await _backend(http, diagnostics=rec).read_balance(_ctx(diagnostics=rec))
    snap = rec.snapshot()
    assert snap["external_user_id"] == "2998032"                    # cached ext id, marked via _player_id
    assert "balance.read" in [s["name"] for s in snap["steps"]]
    assert dict(route.calls.last.request.url.params) == {"id": "2998032"}


@respx.mock
async def test_player_id_fallback_records_resolve_user_id_step_and_marks_external_id():
    _mock_login()
    respx.get(f"{BASE}/api/player/userList").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "count": 1,
                   "data": [{"Id": 99, "id": 99, "Account": "user_no_ext", "score": 0}]}))
    respx.get(f"{BASE}/api/player/agentMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "data": {"balance": 5}}))
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient() as http:
        r = await _backend(http, diagnostics=rec).read_balance(
            _ctx(external=None, username="user_no_ext", diagnostics=rec))
    assert r.balance == 5.0
    snap = rec.snapshot()
    assert snap["external_user_id"] == "99"
    assert "resolve.user_id" in [s["name"] for s in snap["steps"]]
