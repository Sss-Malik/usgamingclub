"""Live-gated end-to-end test against the real VBlink portal.

Skipped unless all of these are set:
  VBLINK_TEST_BASE_URL     e.g. https://gm.vblink777.club/api
  VBLINK_TEST_AGENT_USER   e.g. TestVB159
  VBLINK_TEST_AGENT_PASS   e.g. Test12345
  VBLINK_TEST_PLAYER       must already exist under the agent

VBlink runs the same backend application as UltraPanda — verified byte-identical per
the findings doc §10. This test confirms the alias wiring works end-to-end against the
real host. ~6s sleeps between enterScore calls respect the rate limit.
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
    "VBLINK_TEST_BASE_URL", "VBLINK_TEST_AGENT_USER",
    "VBLINK_TEST_AGENT_PASS", "VBLINK_TEST_PLAYER",
]

pytestmark = pytest.mark.skipif(
    not all(os.getenv(k) for k in _required),
    reason=f"set {', '.join(_required)} to run",
)


@pytest_asyncio.fixture
async def backend():
    base = os.environ["VBLINK_TEST_BASE_URL"]
    user = os.environ["VBLINK_TEST_AGENT_USER"]
    pwd = os.environ["VBLINK_TEST_AGENT_PASS"]
    redis = _fr.FakeRedis(decode_responses=False)
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            client = UltraPandaClient(
                base_url=base, username=user, password=pwd,
                http_client=http, session_store=RedisTokenStore(redis), redis=redis,
                game_id=9998, session_ttl_seconds=1800,
                throttle_ttl_seconds=6, throttle_acquire_timeout_seconds=15.0,
                session_lock_ttl_seconds=10, session_lock_acquire_timeout_seconds=10.0,
                driver_prefix="vblink",
            )
            yield UltraPandaBackend(client)
    finally:
        await redis.aclose()


def _ctx(*, account=None, username=None) -> BackendContext:
    creds = GameCredentials(
        game_id=9998, name="VB Live",
        backend_url=os.environ["VBLINK_TEST_BASE_URL"],
        login_page_url=None,
        backend_username=os.environ["VBLINK_TEST_AGENT_USER"],
        backend_password=os.environ["VBLINK_TEST_AGENT_PASS"],
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="vblink",
    )
    return BackendContext(
        credentials=creds, user_id=1, account=account,
        idempotency_key="live-test", account_username=username,
    )


async def test_live_agent_balance(backend):
    result = await backend.agent_balance(_ctx())
    assert result.agent_balance >= 0


async def test_live_read_balance_for_existing_player(backend):
    player = os.environ["VBLINK_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9998,
        username=player, external_user_id=None,
    )
    result = await backend.read_balance(_ctx(account=account))
    assert result.balance >= 0


async def test_live_recharge_one_dollar_then_redeem_one_dollar(backend):
    player = os.environ["VBLINK_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9998,
        username=player, external_user_id=None,
    )
    ctx = _ctx(account=account)
    before = await backend.read_balance(ctx)
    await backend.recharge(ctx, amount=1)
    await asyncio.sleep(7)
    after_recharge = await backend.read_balance(ctx)
    assert after_recharge.balance == before.balance + 1
    await backend.redeem(ctx, amount=1)
    await asyncio.sleep(7)
    after_redeem = await backend.read_balance(ctx)
    assert after_redeem.balance == before.balance
