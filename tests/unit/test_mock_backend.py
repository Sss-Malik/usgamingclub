# tests/unit/test_mock_backend.py
import pytest

from app.backends.base import BackendError
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.diagnostics import DiagnosticsRecorder
from app.backends.mock.backend import MockBackend


def _creds(game_id=7):
    return GameCredentials(
        game_id=game_id, name="Demo", backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url=None, api_agent_id=None, api_secret_key=None, binding_key=None,
    )


def _ctx(account=True):
    acct = AccountIdentity(game_account_id=1001, user_id=42, game_id=7, username="plyr_42", external_user_id="EXT-42") if account else None
    return BackendContext(credentials=_creds(), user_id=42, account=acct)


async def test_create_account_is_deterministic():
    b = MockBackend()
    r1 = await b.create_account(_ctx(account=False))
    r2 = await b.create_account(_ctx(account=False))
    assert r1.username == r2.username == "mock_42_7"
    assert r1.password and r1.external_user_id


async def test_recharge_echoes_amount_as_balance():
    r = await MockBackend().recharge(_ctx(), amount=50)
    assert r.balance == 50.0


async def test_read_balance_returns_127_5():
    r = await MockBackend().read_balance(_ctx())
    assert r.balance == 127.5


async def test_agent_balance_returns_value():
    r = await MockBackend().agent_balance(_ctx(account=False))
    assert r.agent_balance == 1000.0


async def test_fail_mode_raises_backend_error():
    b = MockBackend(fail=True, fail_reason="boom")
    with pytest.raises(BackendError) as ei:
        await b.read_balance(_ctx())
    assert ei.value.reason == "boom"


def _ctx_diag(rec):
    creds = GameCredentials(game_id=1, name="g", backend_url=None, login_page_url=None,
                            backend_username=None, backend_password=None, api_base_url=None,
                            api_agent_id=None, api_secret_key=None, binding_key=None)
    return BackendContext(credentials=creds, user_id=7, account=None,
                          account_username="u", diagnostics=rec)


async def test_mock_recharge_records_single_build_step():
    rec = DiagnosticsRecorder()
    await MockBackend().recharge(_ctx_diag(rec), amount=5)
    snap = rec.snapshot()
    assert snap["steps"] == [{"name": "recharge.build", "phase": "finalize", "http": False,
                              "external": False, "ok": True, "ms": snap["steps"][0]["ms"]}]
    assert snap["session_reuse"] is None


async def test_mock_recharge_records_build_step_with_ok_false_on_failure():
    rec = DiagnosticsRecorder()
    b = MockBackend(fail=True, fail_reason="test failure")
    with pytest.raises(BackendError):
        await b.recharge(_ctx_diag(rec), amount=5)
    snap = rec.snapshot()
    assert len(snap["steps"]) == 1
    assert snap["steps"][0]["name"] == "recharge.build"
    assert snap["steps"][0]["ok"] is False
    assert snap["session_reuse"] is None
