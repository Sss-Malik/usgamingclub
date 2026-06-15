# tests/unit/test_goldentreasure_backend.py
import json

import httpx
import pytest
import respx

from app.backends.base import BackendError
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.goldentreasure.backend import GoldenTreasureBackend
from app.backends.goldentreasure.client import GoldenTreasureClient
from app.backends.goldentreasure.session import InMemorySessionStore

BASE = "https://gt.test"


def _creds():
    return GameCredentials(
        game_id=13, name="GT",
        backend_url=BASE, login_page_url=None,
        backend_username="Test02Gd1WEB", backend_password="Zaeem@1233",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="goldentreasure",
    )


def _ctx(*, account=True, username="apitest01", idem="idem-1",
         account_username=None, user_id=61):
    acct = AccountIdentity(4001, user_id, 13, username, None) if account else None
    return BackendContext(credentials=_creds(), user_id=user_id, account=acct,
                          idempotency_key=idem, account_username=account_username)


def _backend(http, fake_redis):
    client = GoldenTreasureClient(
        base_url=BASE, username="Test02Gd1WEB", password="Zaeem@1233",
        http_client=http, session_store=InMemorySessionStore(),
        redis=fake_redis, game_id=13,
    )
    return GoldenTreasureBackend(client)


def _login_ok():
    return {"code": 20000, "token": "Ttok", "name": "Test02Gd1WEB", "data": {}}


def _mock_login():
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok()))


# ---- AGENT_BALANCE ----

@respx.mock
async def test_agent_balance_reads_LimitNum(fake_redis):
    _mock_login()
    respx.post(f"{BASE}/api/user/CurScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "LimitNum": "20.00"}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http, fake_redis).agent_balance(_ctx(account=False))
    assert r.agent_balance == 20.0


@respx.mock
async def test_agent_balance_missing_LimitNum_is_terminal(fake_redis):
    _mock_login()
    respx.post(f"{BASE}/api/user/CurScore").mock(return_value=httpx.Response(
        200, json={"code": 20000}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http, fake_redis).agent_balance(_ctx(account=False))
    assert ei.value.reason == "gtreasure:agent_balance_missing"


# ---- READ_BALANCE ----

@respx.mock
async def test_read_balance_posts_account_and_returns_dollars(fake_redis):
    _mock_login()
    route = respx.post(f"{BASE}/api/account/getPlayerScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "curScore": 5}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http, fake_redis).read_balance(_ctx())
    assert r.balance == 5.0
    body = json.loads(route.calls.last.request.content.decode())
    assert body["account"] == "apitest01"
    assert body["token"] == "Ttok"


# ---- CREATE_ACCOUNT ----

@respx.mock
async def test_create_account_posts_username_and_zero_score(fake_redis):
    _mock_login()
    route = respx.post(f"{BASE}/api/account/savePlayer").mock(return_value=httpx.Response(
        200, json={"code": 20000, "message": "新增玩家成功"}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http, fake_redis).create_account(
            _ctx(account=False, account_username="apitestnew")
        )
    assert r.username == "apitestnew"
    assert r.password and r.password.isalnum()
    assert r.external_user_id is None                # spec GT4
    body = json.loads(route.calls.last.request.content.decode())
    assert body["account"] == "apitestnew"
    assert body["score"] == "0"
    assert body["name"] == "" and body["phone"] == "" and body["tel_area_code"] == "" and body["remark"] == ""


@respx.mock
async def test_create_account_requires_account_username(fake_redis):
    _mock_login()
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http, fake_redis).create_account(_ctx(account=False, account_username=None))
    assert ei.value.reason == "account_username_required"


@respx.mock
async def test_create_account_throttles(fake_redis):
    _mock_login()
    respx.post(f"{BASE}/api/account/savePlayer").mock(return_value=httpx.Response(
        200, json={"code": 20000, "message": "ok"}))
    async with httpx.AsyncClient() as http:
        await _backend(http, fake_redis).create_account(_ctx(account=False, account_username="apitestthr"))
    assert await fake_redis.exists("gtreasure_throttle:13") == 1


@respx.mock
async def test_create_account_code_8_is_account_exists(fake_redis):
    _mock_login()
    respx.post(f"{BASE}/api/account/savePlayer").mock(return_value=httpx.Response(
        200, json={"code": 8, "message": "该帐号已被使用"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http, fake_redis).create_account(
                _ctx(account=False, account_username="taken")
            )
    assert ei.value.reason == "gtreasure:account_exists"


# ---- RECHARGE ----

@respx.mock
async def test_recharge_sends_positive_score_and_throttles(fake_redis):
    _mock_login()
    route = respx.post(f"{BASE}/api/account/enterScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "message": "进分成功"}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http, fake_redis).recharge(_ctx(), amount=50)
    body = json.loads(route.calls.last.request.content.decode())
    assert body["account"] == "apitest01"
    assert body["score"] == "50"                     # wire value "50"
    assert body["user_type"] == "player"
    assert body["remark"] == ""
    assert r.balance is None                   # RechargeResult() with no balance
    assert await fake_redis.exists("gtreasure_throttle:13") == 1


# ---- REDEEM ----

@respx.mock
async def test_redeem_sends_negative_score_and_throttles(fake_redis):
    _mock_login()
    route = respx.post(f"{BASE}/api/account/enterScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "message": "下分成功"}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http, fake_redis).redeem(_ctx(), amount=50)
    body = json.loads(route.calls.last.request.content.decode())
    assert body["score"] == "-50"                    # negative wire value "-50"
    assert body["account"] == "apitest01"
    assert r.balance is None                   # RedeemResult() with no balance


@respx.mock
async def test_redeem_code_21_is_operation_refused(fake_redis):
    _mock_login()
    respx.post(f"{BASE}/api/account/enterScore").mock(return_value=httpx.Response(
        200, json={"code": 21, "message": "充值失败：服务器维护中", "test": 21}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http, fake_redis).redeem(_ctx(), amount=1)
    assert ei.value.reason == "gtreasure:operation_refused"


# ---- RESET_PASSWORD ----

@respx.mock
async def test_reset_password_posts_to_updatePlayer_and_does_not_throttle(fake_redis):
    _mock_login()
    route = respx.post(f"{BASE}/api/account/updatePlayer").mock(return_value=httpx.Response(
        200, json={"code": 20000, "message": "编辑玩家成功", "info": {}}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http, fake_redis).reset_password(_ctx())
    assert r.password and r.password.isalnum()
    body = json.loads(route.calls.last.request.content.decode())
    assert body["account"] == "apitest01"
    assert body["pwd"] == r.password
    # RESET_PASSWORD is NOT throttled (spec GT7) — throttle key must NOT be set.
    assert await fake_redis.exists("gtreasure_throttle:13") == 0
