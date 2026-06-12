import time

import httpx
import pytest
import respx

from app.backends.base import BackendError, TransientBackendError
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.ultrapanda.backend import UltraPandaBackend
from app.backends.ultrapanda.client import UltraPandaClient
from app.backends.ultrapanda.session import CachedSession, InMemoryTokenStore

BASE = "https://up.test"


def _credentials() -> GameCredentials:
    return GameCredentials(
        game_id=42, name="UP Test",
        backend_url=BASE, login_page_url=None,
        backend_username="TestUP159", backend_password="Test1234",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="ultrapanda",
    )


def _account(username: str = "userup01") -> AccountIdentity:
    return AccountIdentity(
        game_account_id=1, user_id=1, game_id=42,
        username=username, external_user_id=None,
    )


def _ctx(*, account=None, username=None) -> BackendContext:
    return BackendContext(
        credentials=_credentials(), user_id=1, account=account,
        idempotency_key="idem", account_username=username,
    )


def _make_backend(http, fake_redis):
    store = InMemoryTokenStore()
    client = UltraPandaClient(
        base_url=BASE, username="u", password="p",
        http_client=http, session_store=store, redis=fake_redis,
        game_id=42, session_ttl_seconds=1800,
        throttle_ttl_seconds=6, throttle_acquire_timeout_seconds=2.0,
        session_lock_ttl_seconds=10, session_lock_acquire_timeout_seconds=2.0,
        driver_prefix="ultrapanda",
    )
    return UltraPandaBackend(client), store


async def _seed_session(store):
    await store.set(42, CachedSession(token="testtok", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)


# --- agent_balance ---

@respx.mock
async def test_agent_balance_returns_LimitNum_as_cents(fake_redis):
    respx.post(f"{BASE}/user/CurScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "LimitNum": "3.00"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        result = await backend.agent_balance(_ctx())
    assert result.agent_balance_cents == 300


# --- read_balance ---

@respx.mock
async def test_read_balance_returns_curScore_as_cents(fake_redis):
    respx.post(f"{BASE}/account/getPlayerScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "curScore": 1.50})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        result = await backend.read_balance(_ctx(account=_account("u01")))
    assert result.balance_cents == 150


@respx.mock
async def test_read_balance_unknown_account_raises_terminal(fake_redis):
    respx.post(f"{BASE}/account/getPlayerScore").mock(
        return_value=httpx.Response(200, json={"code": 22, "message": "转入账号不存在"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        with pytest.raises(BackendError) as ei:
            await backend.read_balance(_ctx(account=_account("nope")))
    assert ei.value.reason == "ultrapanda:player_not_found"


# --- create_account ---

@respx.mock
async def test_create_account_posts_savePlayer_and_returns_credentials(fake_redis):
    route = respx.post(f"{BASE}/account/savePlayer").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "新增玩家成功"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        result = await backend.create_account(_ctx(username="newuser01"))
    assert result.username == "newuser01"
    assert result.password
    sent_body = route.calls.last.request.content.decode()
    assert '"account": "newuser01"' in sent_body or '"account":"newuser01"' in sent_body


@respx.mock
async def test_create_account_duplicate_raises_terminal(fake_redis):
    respx.post(f"{BASE}/account/savePlayer").mock(
        return_value=httpx.Response(200, json={"code": 8, "message": "该帐号已被使用"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        with pytest.raises(BackendError) as ei:
            await backend.create_account(_ctx(username="existing"))
    assert ei.value.reason == "ultrapanda:account_exists"


# --- reset_password ---

@respx.mock
async def test_reset_password_sends_all_required_fields_and_returns_pwd(fake_redis):
    route = respx.post(f"{BASE}/account/updatePlayer").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "编辑玩家成功",
                                               "info": {"Account": "u01"}})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        result = await backend.reset_password(_ctx(account=_account("u01")))
    assert result.password
    body = route.calls.last.request.content.decode()
    for k in ("account", "pwd", "name", "tel_area_code", "phone", "remark"):
        assert f'"{k}"' in body


# --- recharge ---

@respx.mock
async def test_recharge_sends_total_credit_cents_as_score(fake_redis):
    """Regression guard: must send total_credit_cents (principal + bonus), not amount_cents."""
    route = respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "进分成功"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        await backend.recharge(
            _ctx(account=_account("u01")),
            amount_cents=1200, bonus_cents=1200, total_credit_cents=2400,
        )
    body = route.calls.last.request.content.decode()
    assert '"score": "24.00"' in body or '"score":"24.00"' in body
    assert '"score": "12.00"' not in body and '"score":"12.00"' not in body
    assert '"user_type": 0' in body or '"user_type":0' in body


@respx.mock
async def test_recharge_insufficient_agent_funds_raises_terminal(fake_redis):
    respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 21, "message": "充值失败：服务器维护中",
                                               "test": 21})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        with pytest.raises(BackendError) as ei:
            await backend.recharge(
                _ctx(account=_account("u01")),
                amount_cents=100, bonus_cents=0, total_credit_cents=100,
            )
    assert ei.value.reason == "ultrapanda:insufficient_agent_funds"


# --- redeem ---

@respx.mock
async def test_redeem_sends_negative_score(fake_redis):
    route = respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "下分成功"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        await backend.redeem(_ctx(account=_account("u01")), amount_cents=150)
    body = route.calls.last.request.content.decode()
    assert '"score": "-1.50"' in body or '"score":"-1.50"' in body


@respx.mock
async def test_redeem_insufficient_player_credit_raises_terminal(fake_redis):
    respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 21, "message": "充值失败：服务器维护中"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        with pytest.raises(BackendError) as ei:
            await backend.redeem(_ctx(account=_account("u01")), amount_cents=999)
    assert ei.value.reason == "ultrapanda:insufficient_player_credit"


# --- rate-limit ---

@respx.mock
async def test_recharge_167_rate_limit_is_transient(fake_redis):
    respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 167, "message": "high frequency request"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        with pytest.raises(TransientBackendError, match="rate_limited"):
            await backend.recharge(
                _ctx(account=_account("u01")),
                amount_cents=100, bonus_cents=0, total_credit_cents=100,
            )
