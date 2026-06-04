import pytest

from app.preflight.checks import PreflightError, build_context


async def test_account_scoped_context_loads_account(seeded):
    async with seeded() as s:
        ctx = await build_context(
            s, type="READ_BALANCE", user_id=42, game_id=7, game_account_id=1001
        )
    assert ctx.credentials.api_agent_id == "agent-1"
    assert ctx.account is not None and ctx.account.username == "plyr_42"


async def test_create_account_has_no_account(seeded):
    async with seeded() as s:
        ctx = await build_context(
            s, type="CREATE_ACCOUNT", user_id=42, game_id=7, game_account_id=None
        )
    assert ctx.account is None
    assert ctx.user_id == 42


async def test_missing_game_raises(seeded):
    async with seeded() as s:
        with pytest.raises(PreflightError) as ei:
            await build_context(s, type="AGENT_BALANCE", user_id=None, game_id=999, game_account_id=None)
    assert "game_not_found" in ei.value.reason


async def test_missing_account_raises(seeded):
    async with seeded() as s:
        with pytest.raises(PreflightError) as ei:
            await build_context(s, type="REDEEM", user_id=42, game_id=7, game_account_id=999)
    assert "game_account_not_found" in ei.value.reason
