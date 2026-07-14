# tests/unit/test_gamevault_backend.py
import httpx
import pytest
import respx

from app.backends.base import BackendError, TransientBackendError
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.diagnostics import DiagnosticsRecorder
from app.backends.gamevault.backend import GameVaultBackend, _to_dollars_str
from app.backends.gamevault.client import GameVaultClient

BASE = "https://gv.test"


def _creds():
    return GameCredentials(
        game_id=9, name="GV", backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url=BASE, api_agent_id="11", api_secret_key="gvsecret",
        binding_key=None, backend_driver="gamevault",
    )


def _ctx(*, account=True, external="88880212", username="user020301", idem="idem-1",
         account_username=None, diagnostics=None):
    acct = AccountIdentity(2001, 43, 9, username, external) if account else None
    return BackendContext(credentials=_creds(), user_id=43, account=acct,
                          idempotency_key=idem, account_username=account_username,
                          diagnostics=diagnostics)


def _backend(http, diagnostics=None):
    return GameVaultBackend(GameVaultClient(base_url=BASE, agent_id="11", secret_key="gvsecret",
                                             http_client=http, diagnostics=diagnostics))


def test_unit_helpers():
    assert _to_dollars_str(50) == "50"
    assert _to_dollars_str(100) == "100"
    assert _to_dollars_str(1) == "1"


@respx.mock
async def test_create_account_missing_user_id_is_transient():
    # code:0 but no user_id -> transient (not cached), so a re-run can recover.
    respx.post(f"{BASE}/api/external/addUser").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {}, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _backend(http).create_account(_ctx(account=False, account_username="usr_43"))


@respx.mock
async def test_read_balance_returns_dollars():
    respx.post(f"{BASE}/api/external/userBalance").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"user_balance": "127.5"}, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        r = await _backend(http).read_balance(_ctx())
    assert r.balance == 127.5


@respx.mock
async def test_recharge_sends_dollars_str_and_order_id():
    route = respx.post(f"{BASE}/api/external/recharge").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"user_balance": "150.0"}, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        r = await _backend(http).recharge(_ctx(), amount=50)
    body = route.calls.last.request.content.decode()
    assert 'name="amount"' in body and "50" in body
    assert 'name="order_id"' in body and "idem-1" in body
    assert r.balance == 150.0


@respx.mock
async def test_redeem_user_in_game_raises_backend_error():
    respx.post(f"{BASE}/api/external/withdraw").mock(
        return_value=httpx.Response(200, json={"code": 10, "msg": "User is in game", "data": None, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http).redeem(_ctx(), amount=30)
    assert ei.value.reason == "gamevault:10:user_in_game"


@respx.mock
async def test_reset_password_returns_generated_password():
    route = respx.post(f"{BASE}/api/external/resetPassword").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": None, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        r = await _backend(http).reset_password(_ctx())
    assert r.password and r.password.isalnum()
    assert 'name="login_pwd"' in route.calls.last.request.content.decode()


@respx.mock
async def test_create_account_requires_username():
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http).create_account(_ctx(account=False, account_username=None))
    assert ei.value.reason == "account_username_required"


@respx.mock
async def test_create_account_posts_username_and_returns_user_id():
    route = respx.post(f"{BASE}/api/external/addUser").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"account_name": "usr_43", "user_id": "88886468"}, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        r = await _backend(http).create_account(_ctx(account=False, account_username="usr_43"))
    assert r.username == "usr_43" and r.external_user_id == "88886468" and r.password.isalnum()
    body = route.calls.last.request.content.decode()
    assert 'name="account"' in body and "usr_43" in body


@respx.mock
async def test_user_id_falls_back_to_getUserID_when_external_missing():
    respx.post(f"{BASE}/api/external/getUserID").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"user_id": "88880212"}, "count": 0})
    )
    bal = respx.post(f"{BASE}/api/external/userBalance").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"user_balance": "5.0"}, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        r = await _backend(http).read_balance(_ctx(external=None, username="user_no_ext"))
    assert r.balance == 5.0
    assert 'name="user_id"' in bal.calls.last.request.content.decode()  # resolved id used downstream


@respx.mock
async def test_agent_balance_returns_dollars():
    respx.post(f"{BASE}/api/external/agentBalance").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"agent_balance": "3649.0057"}, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        r = await _backend(http).agent_balance(_ctx(account=False))
    assert r.agent_balance == 3649.0057


@respx.mock
async def test_recharge_records_resolve_and_primary_steps():
    respx.post(f"{BASE}/api/external/getUserID").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"user_id": "88880212"}, "count": 0})
    )
    respx.post(f"{BASE}/api/external/recharge").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"user_balance": "150.0"}, "count": 0})
    )
    rec = DiagnosticsRecorder()
    ctx = _ctx(external=None, username="u", diagnostics=rec)
    async with httpx.AsyncClient() as http:
        await _backend(http, diagnostics=rec).recharge(ctx, amount=5)
    names = [s["name"] for s in rec.snapshot()["steps"]]
    assert names == ["resolve.user_id", "recharge.post"]
    assert rec.snapshot()["external_user_id"] == "88880212"
    assert rec.snapshot()["balance_after"] == 150.0


@respx.mock
async def test_recharge_with_cached_external_user_id_skips_resolve_step():
    respx.post(f"{BASE}/api/external/recharge").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"user_balance": "150.0"}, "count": 0})
    )
    rec = DiagnosticsRecorder()
    ctx = _ctx(diagnostics=rec)  # external_user_id already cached -> no resolve call
    async with httpx.AsyncClient() as http:
        await _backend(http, diagnostics=rec).recharge(ctx, amount=5)
    names = [s["name"] for s in rec.snapshot()["steps"]]
    assert names == ["recharge.post"]
    assert rec.snapshot()["external_user_id"] == "88880212"


@respx.mock
async def test_create_account_marks_external_user_id_and_records_addUser_step():
    respx.post(f"{BASE}/api/external/addUser").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"account_name": "usr_43", "user_id": "88886468"}, "count": 0})
    )
    rec = DiagnosticsRecorder()
    ctx = _ctx(account=False, account_username="usr_43", diagnostics=rec)
    async with httpx.AsyncClient() as http:
        await _backend(http, diagnostics=rec).create_account(ctx)
    names = [s["name"] for s in rec.snapshot()["steps"]]
    assert names == ["addUser.post"]
    assert rec.snapshot()["external_user_id"] == "88886468"


@respx.mock
async def test_read_balance_records_balance_read_step_with_no_balance_mark():
    respx.post(f"{BASE}/api/external/userBalance").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"user_balance": "127.5"}, "count": 0})
    )
    rec = DiagnosticsRecorder()
    ctx = _ctx(diagnostics=rec)
    async with httpx.AsyncClient() as http:
        r = await _backend(http, diagnostics=rec).read_balance(ctx)
    assert r.balance == 127.5
    names = [s["name"] for s in rec.snapshot()["steps"]]
    assert names == ["balance.read"]
    # read_balance never marks balance_after: the value already flows via ReadBalanceResult.
    assert rec.snapshot()["balance_after"] is None


@respx.mock
async def test_reset_password_records_reset_post_step():
    respx.post(f"{BASE}/api/external/resetPassword").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": None, "count": 0})
    )
    rec = DiagnosticsRecorder()
    ctx = _ctx(diagnostics=rec)
    async with httpx.AsyncClient() as http:
        await _backend(http, diagnostics=rec).reset_password(ctx)
    names = [s["name"] for s in rec.snapshot()["steps"]]
    assert names == ["reset.post"]


@respx.mock
async def test_redeem_records_withdraw_post_step_and_marks_balance_after():
    respx.post(f"{BASE}/api/external/withdraw").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"user_balance": "20.0"}, "count": 0})
    )
    rec = DiagnosticsRecorder()
    ctx = _ctx(diagnostics=rec)
    async with httpx.AsyncClient() as http:
        r = await _backend(http, diagnostics=rec).redeem(ctx, amount=30)
    assert r.balance == 20.0
    names = [s["name"] for s in rec.snapshot()["steps"]]
    assert names == ["withdraw.post"]
    assert rec.snapshot()["balance_after"] == 20.0
