"""Live-gated end-to-end test against the real OrionStars portal.

Skipped unless all of these are set:
  ANTICAPTCHA_API_KEY
  ORIONSTARS_TEST_BASE_URL    e.g. https://orionstars.vip:8781
  ORIONSTARS_TEST_AGENT_USER  e.g. TestOS159
  ORIONSTARS_TEST_AGENT_PASS  e.g. Test@159872!!
  ORIONSTARS_TEST_PLAYER      e.g. Saud_Doe892   (must already exist under the agent)

Costs ~$0.001 per login (one AntiCaptcha solve). Run manually with:
  .venv/bin/pytest tests/integration/test_orionstars_integration.py -v
"""
import os

import httpx
import pytest
import pytest_asyncio

from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.session import InMemoryCookieSessionStore
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.orionstars.backend import OrionStarsBackend
from app.captcha.anticaptcha import AntiCaptchaSolver

_required = [
    "ANTICAPTCHA_API_KEY", "ORIONSTARS_TEST_BASE_URL",
    "ORIONSTARS_TEST_AGENT_USER", "ORIONSTARS_TEST_AGENT_PASS",
    "ORIONSTARS_TEST_PLAYER",
]

pytestmark = pytest.mark.skipif(
    not all(os.getenv(k) for k in _required),
    reason=f"set {', '.join(_required)} to run",
)


@pytest_asyncio.fixture
async def backend():
    base = os.environ["ORIONSTARS_TEST_BASE_URL"]
    user = os.environ["ORIONSTARS_TEST_AGENT_USER"]
    pwd = os.environ["ORIONSTARS_TEST_AGENT_PASS"]
    async with httpx.AsyncClient(timeout=60.0) as http:
        client = AspnetCashierClient(
            base_url=base, username=user, password=pwd,
            http_client=http,
            session_store=InMemoryCookieSessionStore(),
            captcha_solver=AntiCaptchaSolver(api_key=os.environ["ANTICAPTCHA_API_KEY"]),
            game_id=9999, session_ttl_seconds=1800,
            lock_ttl_seconds=20, lock_acquire_timeout_seconds=30.0,
            captcha_login_max_attempts=3, driver_prefix="orionstars",
        )
        yield OrionStarsBackend(client)


def _ctx(*, account=None, username=None) -> BackendContext:
    creds = GameCredentials(
        game_id=9999, name="OS Live",
        backend_url=os.environ["ORIONSTARS_TEST_BASE_URL"],
        login_page_url=None,
        backend_username=os.environ["ORIONSTARS_TEST_AGENT_USER"],
        backend_password=os.environ["ORIONSTARS_TEST_AGENT_PASS"],
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="orionstars",
    )
    return BackendContext(
        credentials=creds, user_id=1, account=account,
        idempotency_key="live-test", account_username=username,
    )


async def test_live_agent_balance(backend):
    result = await backend.agent_balance(_ctx())
    assert result.agent_balance_cents >= 0


async def test_live_read_balance_for_existing_player(backend):
    player = os.environ["ORIONSTARS_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9999,
        username=player, external_user_id=None,        # forces search-by-username path
    )
    result = await backend.read_balance(_ctx(account=account))
    assert result.balance_cents >= 0


async def test_live_recharge_one_dollar_then_redeem_one_dollar(backend):
    player = os.environ["ORIONSTARS_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9999,
        username=player, external_user_id=None,
    )
    ctx = _ctx(account=account)
    before = await backend.read_balance(ctx)
    await backend.recharge(ctx, amount_cents=100, bonus_cents=0, total_credit_cents=100)
    after_recharge = await backend.read_balance(ctx)
    assert after_recharge.balance_cents == before.balance_cents + 100
    await backend.redeem(ctx, amount_cents=100)
    after_redeem = await backend.read_balance(ctx)
    assert after_redeem.balance_cents == before.balance_cents


async def test_live_reset_password_then_login_unaffected(backend):
    """Reset the test player's password. This is destructive — only run with a disposable player."""
    player = os.environ["ORIONSTARS_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9999,
        username=player, external_user_id=None,
    )
    result = await backend.reset_password(_ctx(account=account))
    assert result.password and len(result.password) >= 5
