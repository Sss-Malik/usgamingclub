"""Live-gated end-to-end test against the real MilkyWay portal.

Skipped unless all of these are set:
  ANTICAPTCHA_API_KEY
  MILKYWAY_TEST_BASE_URL      e.g. https://milkywayapp.xyz:8781
  MILKYWAY_TEST_AGENT_USER    e.g. TestMW159
  MILKYWAY_TEST_AGENT_PASS
  MILKYWAY_TEST_PLAYER

Costs ~$0.001 per login (one AntiCaptcha solve). Run manually with:
  .venv/bin/pytest tests/integration/test_milkyway_integration.py -v
"""
import os

import httpx
import pytest
import pytest_asyncio

from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.session import InMemoryCookieSessionStore
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.milkyway.backend import MilkyWayBackend
from app.captcha.anticaptcha import AntiCaptchaSolver

_required = [
    "ANTICAPTCHA_API_KEY", "MILKYWAY_TEST_BASE_URL",
    "MILKYWAY_TEST_AGENT_USER", "MILKYWAY_TEST_AGENT_PASS",
    "MILKYWAY_TEST_PLAYER",
]

pytestmark = pytest.mark.skipif(
    not all(os.getenv(k) for k in _required),
    reason=f"set {', '.join(_required)} to run",
)


@pytest_asyncio.fixture
async def backend():
    base = os.environ["MILKYWAY_TEST_BASE_URL"]
    user = os.environ["MILKYWAY_TEST_AGENT_USER"]
    pwd = os.environ["MILKYWAY_TEST_AGENT_PASS"]
    async with httpx.AsyncClient(timeout=60.0) as http:
        client = AspnetCashierClient(
            base_url=base, username=user, password=pwd,
            http_client=http,
            session_store=InMemoryCookieSessionStore(),
            captcha_solver=AntiCaptchaSolver(api_key=os.environ["ANTICAPTCHA_API_KEY"]),
            game_id=9998, session_ttl_seconds=1800,
            lock_ttl_seconds=20, lock_acquire_timeout_seconds=30.0,
            captcha_login_max_attempts=3, driver_prefix="milkyway",
        )
        yield MilkyWayBackend(client)


def _ctx(*, account=None, username=None) -> BackendContext:
    creds = GameCredentials(
        game_id=9998, name="MW Live",
        backend_url=os.environ["MILKYWAY_TEST_BASE_URL"],
        login_page_url=None,
        backend_username=os.environ["MILKYWAY_TEST_AGENT_USER"],
        backend_password=os.environ["MILKYWAY_TEST_AGENT_PASS"],
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="milkyway",
    )
    return BackendContext(
        credentials=creds, user_id=1, account=account,
        idempotency_key="live-test", account_username=username,
    )


async def test_live_agent_balance(backend):
    result = await backend.agent_balance(_ctx())
    assert result.agent_balance >= 0


async def test_live_read_balance_for_existing_player(backend):
    player = os.environ["MILKYWAY_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9998,
        username=player, external_user_id=None,
    )
    result = await backend.read_balance(_ctx(account=account))
    assert result.balance >= 0


async def test_live_recharge_one_dollar_then_redeem_one_dollar(backend):
    player = os.environ["MILKYWAY_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9998,
        username=player, external_user_id=None,
    )
    ctx = _ctx(account=account)
    before = await backend.read_balance(ctx)
    await backend.recharge(ctx, amount=1)
    after_recharge = await backend.read_balance(ctx)
    assert after_recharge.balance == before.balance + 1
    await backend.redeem(ctx, amount=1)
    after_redeem = await backend.read_balance(ctx)
    assert after_redeem.balance == before.balance
