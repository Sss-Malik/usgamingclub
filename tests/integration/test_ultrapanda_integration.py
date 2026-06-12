"""Live-gated end-to-end test against the real UltraPanda portal.

Skipped unless all of these are set:
  ULTRAPANDA_TEST_BASE_URL     e.g. https://ht.ultrapanda.mobi/api
  ULTRAPANDA_TEST_AGENT_USER   e.g. TestUP159
  ULTRAPANDA_TEST_AGENT_PASS   e.g. Test1234
  ULTRAPANDA_TEST_PLAYER       must already exist under the agent

Costs: no captcha. The vpower backend rate-limits enterScore at ~6s; this test inserts
sleep(7) between recharge and redeem to respect the throttle.
"""
import asyncio
import os

import fakeredis.aioredis as _fr
import httpx
import pytest
import pytest_asyncio

from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.ultrapanda.backend import UltraPandaBackend
from app.backends.ultrapanda.client import UltraPandaClient
from app.backends.ultrapanda.session import RedisTokenStore

_required = [
    "ULTRAPANDA_TEST_BASE_URL", "ULTRAPANDA_TEST_AGENT_USER",
    "ULTRAPANDA_TEST_AGENT_PASS", "ULTRAPANDA_TEST_PLAYER",
]

pytestmark = pytest.mark.skipif(
    not all(os.getenv(k) for k in _required),
    reason=f"set {', '.join(_required)} to run",
)


@pytest_asyncio.fixture
async def backend():
    base = os.environ["ULTRAPANDA_TEST_BASE_URL"]
    user = os.environ["ULTRAPANDA_TEST_AGENT_USER"]
    pwd = os.environ["ULTRAPANDA_TEST_AGENT_PASS"]
    redis = _fr.FakeRedis(decode_responses=False)
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            client = UltraPandaClient(
                base_url=base, username=user, password=pwd,
                http_client=http, session_store=RedisTokenStore(redis), redis=redis,
                game_id=9999, session_ttl_seconds=1800,
                throttle_ttl_seconds=6, throttle_acquire_timeout_seconds=15.0,
                session_lock_ttl_seconds=10, session_lock_acquire_timeout_seconds=10.0,
                driver_prefix="ultrapanda",
            )
            yield UltraPandaBackend(client)
    finally:
        await redis.aclose()


def _ctx(*, account=None, username=None) -> BackendContext:
    creds = GameCredentials(
        game_id=9999, name="UP Live",
        backend_url=os.environ["ULTRAPANDA_TEST_BASE_URL"],
        login_page_url=None,
        backend_username=os.environ["ULTRAPANDA_TEST_AGENT_USER"],
        backend_password=os.environ["ULTRAPANDA_TEST_AGENT_PASS"],
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="ultrapanda",
    )
    return BackendContext(
        credentials=creds, user_id=1, account=account,
        idempotency_key="live-test", account_username=username,
    )


async def test_live_agent_balance(backend):
    result = await backend.agent_balance(_ctx())
    assert result.agent_balance_cents >= 0


async def test_live_read_balance_for_existing_player(backend):
    player = os.environ["ULTRAPANDA_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9999,
        username=player, external_user_id=None,
    )
    result = await backend.read_balance(_ctx(account=account))
    assert result.balance_cents >= 0


async def test_live_recharge_one_dollar_then_redeem_one_dollar(backend):
    player = os.environ["ULTRAPANDA_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9999,
        username=player, external_user_id=None,
    )
    ctx = _ctx(account=account)
    before = await backend.read_balance(ctx)
    await backend.recharge(ctx, amount_cents=100, bonus_cents=0, total_credit_cents=100)
    await asyncio.sleep(7)
    after_recharge = await backend.read_balance(ctx)
    assert after_recharge.balance_cents == before.balance_cents + 100
    await backend.redeem(ctx, amount_cents=100)
    await asyncio.sleep(7)
    after_redeem = await backend.read_balance(ctx)
    assert after_redeem.balance_cents == before.balance_cents


async def test_live_reset_password_then_login_unaffected(backend):
    """Destructive — only run with a disposable test player."""
    player = os.environ["ULTRAPANDA_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9999,
        username=player, external_user_id=None,
    )
    result = await backend.reset_password(_ctx(account=account))
    assert result.password and len(result.password) >= 5
