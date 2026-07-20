import time

import httpx
import pytest
import respx

from app.backends.base import BackendError, TransientBackendError
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.diagnostics import DiagnosticsRecorder
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


def _ctx(*, account=None, username=None, diagnostics=None) -> BackendContext:
    return BackendContext(
        credentials=_credentials(), user_id=1, account=account,
        idempotency_key="idem", account_username=username,
        diagnostics=diagnostics,
    )


def _make_backend(http, fake_redis, *, diagnostics=None):
    store = InMemoryTokenStore()
    client = UltraPandaClient(
        base_url=BASE, username="u", password="p",
        http_client=http, session_store=store, redis=fake_redis,
        game_id=42, session_ttl_seconds=1800,
        throttle_ttl_seconds=6, throttle_acquire_timeout_seconds=2.0,
        session_lock_ttl_seconds=10, session_lock_acquire_timeout_seconds=2.0,
        driver_prefix="ultrapanda",
        diagnostics=diagnostics,
    )
    return UltraPandaBackend(client), store


async def _seed_session(store):
    await store.set(42, CachedSession(token="testtok", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)


# --- agent_balance ---

@respx.mock
async def test_agent_balance_returns_LimitNum_as_dollars(fake_redis):
    respx.post(f"{BASE}/user/CurScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "LimitNum": "3.00"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        result = await backend.agent_balance(_ctx())
    assert result.agent_balance == 3.0


# --- read_balance ---

@respx.mock
async def test_read_balance_returns_curScore_as_dollars(fake_redis):
    respx.post(f"{BASE}/account/getPlayerScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "curScore": 127.50})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        result = await backend.read_balance(_ctx(account=_account("u01")))
    assert result.balance == 127.5


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
async def test_recharge_sends_amount_as_score_with_two_decimal_places(fake_redis):
    """Wire value must be '50.00' for amount=50."""
    route = respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "进分成功"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        await backend.recharge(_ctx(account=_account("u01")), amount=50)
    body = route.calls.last.request.content.decode()
    assert '"score": "50.00"' in body or '"score":"50.00"' in body
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
            await backend.recharge(_ctx(account=_account("u01")), amount=1)
    assert ei.value.reason == "ultrapanda:insufficient_agent_funds"


@respx.mock
async def test_recharge_self_heals_when_session_died_with_code_52(fake_redis):
    """End-to-end money-op guarantee: a recharge whose cached session has died (code 52)
    re-logs-in and retries once, then succeeds — so the live no_permission burst self-heals.
    The score POST runs exactly twice (once rejected pre-relogin, once applied), so money
    can move at most once."""
    respx.post(f"{BASE}/user/login").mock(
        return_value=httpx.Response(200, json={"code": 20000, "token": "FRESH"})
    )
    route = respx.post(f"{BASE}/account/enterScore").mock(
        side_effect=[
            httpx.Response(200, json={"code": 52, "message": "no permission"}),
            httpx.Response(200, json={"code": 20000, "message": "进分成功"}),
        ]
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        result = await backend.recharge(_ctx(account=_account("u01")), amount=50)
    assert result.balance is None       # recharge success sentinel
    assert route.call_count == 2


@respx.mock
async def test_recharge_persistent_52_after_relogin_raises_terminal_no_permission(fake_redis):
    """A genuine, persistent permission error (fresh session still 52) must surface as
    terminal `no_permission`, not be masked as transient."""
    respx.post(f"{BASE}/user/login").mock(
        return_value=httpx.Response(200, json={"code": 20000, "token": "FRESH"})
    )
    respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 52, "message": "no permission"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        with pytest.raises(BackendError) as ei:
            await backend.recharge(_ctx(account=_account("u01")), amount=50)
    assert ei.value.reason == "ultrapanda:no_permission"
    assert not isinstance(ei.value, TransientBackendError)


# --- redeem ---

@respx.mock
async def test_redeem_sends_negative_score(fake_redis):
    route = respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "下分成功"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        await backend.redeem(_ctx(account=_account("u01")), amount=50)
    body = route.calls.last.request.content.decode()
    assert '"score": "-50.00"' in body or '"score":"-50.00"' in body


@respx.mock
async def test_redeem_insufficient_player_credit_raises_terminal(fake_redis):
    respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 21, "message": "充值失败：服务器维护中"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        with pytest.raises(BackendError) as ei:
            await backend.redeem(_ctx(account=_account("u01")), amount=999)
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
            await backend.recharge(_ctx(account=_account("u01")), amount=1)


# --- diagnostics: provider_code on the backend's business-code raise (no message) ---

@respx.mock
async def test_recharge_business_error_carries_provider_code_no_message(fake_redis):
    respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 21, "message": "充值失败：服务器维护中"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        with pytest.raises(BackendError) as ei:
            await backend.recharge(_ctx(account=_account("u01")), amount=1)
    assert ei.value.reason == "ultrapanda:insufficient_agent_funds"
    assert ei.value.provider_http_status == 200
    assert ei.value.provider_code == 21
    assert ei.value.provider_message is None          # the vpower API returns no message field


@respx.mock
async def test_read_balance_business_error_carries_provider_code_transient(fake_redis):
    respx.post(f"{BASE}/account/getPlayerScore").mock(
        return_value=httpx.Response(200, json={"code": 167, "message": "high frequency request"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        with pytest.raises(TransientBackendError) as ei:
            await backend.read_balance(_ctx(account=_account("u01")))
    assert ei.value.provider_http_status == 200
    assert ei.value.provider_code == 167
    assert ei.value.provider_message is None


# --- diagnostics: named steps recorded through the backend ----

@respx.mock
async def test_create_account_records_create_post_step(fake_redis):
    respx.post(f"{BASE}/account/savePlayer").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "新增玩家成功"})
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis, diagnostics=rec)
        await _seed_session(store)
        await backend.create_account(_ctx(username="newuser01"))
    names = [s["name"] for s in rec.snapshot()["steps"]]
    assert "create.post" in names


@respx.mock
async def test_read_balance_records_balance_read_step(fake_redis):
    respx.post(f"{BASE}/account/getPlayerScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "curScore": 5})
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis, diagnostics=rec)
        await _seed_session(store)
        await backend.read_balance(_ctx(account=_account("u01")))
    names = [s["name"] for s in rec.snapshot()["steps"]]
    assert "balance.read" in names
    assert "throttle.acquire" not in names               # read is not a mutating op


@respx.mock
async def test_reset_password_records_reset_post_step_without_throttle(fake_redis):
    respx.post(f"{BASE}/account/updatePlayer").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "编辑玩家成功",
                                               "info": {"Account": "u01"}})
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis, diagnostics=rec)
        await _seed_session(store)
        await backend.reset_password(_ctx(account=_account("u01")))
    names = [s["name"] for s in rec.snapshot()["steps"]]
    assert "reset.post" in names
    assert "throttle.acquire" not in names


@respx.mock
async def test_recharge_records_throttle_and_recharge_post_steps(fake_redis):
    respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "进分成功"})
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis, diagnostics=rec)
        await _seed_session(store)
        await backend.recharge(_ctx(account=_account("u01")), amount=50)
    names = [s["name"] for s in rec.snapshot()["steps"]]
    assert "throttle.acquire" in names
    assert "recharge.post" in names


@respx.mock
async def test_redeem_records_throttle_and_redeem_post_steps(fake_redis):
    respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "下分成功"})
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis, diagnostics=rec)
        await _seed_session(store)
        await backend.redeem(_ctx(account=_account("u01")), amount=30)
    names = [s["name"] for s in rec.snapshot()["steps"]]
    assert "throttle.acquire" in names
    assert "redeem.post" in names


@respx.mock
async def test_backend_never_marks_external_user_id_or_balance(fake_redis):
    # savePlayer returns no uid and enterScore returns no balance; getPlayerScore flows via
    # user_data rather than a diag mark. The backend must not call ctx.diag.mark_* at all for
    # ultrapanda. Sharing one recorder between ctx and the client (as the real executor does)
    # means a stray mark_* call anywhere would show up here.
    respx.post(f"{BASE}/account/savePlayer").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "新增玩家成功"})
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis, diagnostics=rec)
        await _seed_session(store)
        ctx = _ctx(username="markstest", diagnostics=rec)
        await backend.create_account(ctx)
    snap = rec.snapshot()
    assert snap["external_user_id"] is None
    assert snap["balance_after"] is None
    assert snap["balance_before"] is None
