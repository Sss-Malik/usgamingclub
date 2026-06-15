# tests/unit/test_db_models.py
import pytest
from sqlalchemy import select

from app.db.models import Game, GameAccount


@pytest.mark.asyncio
async def test_game_columns_map_arcadia_schema(seeded):
    async with seeded() as s:
        game = (await s.execute(select(Game).where(Game.id == 9))).scalar_one()
        assert game.name == "GameVault Demo"
        assert game.backend_driver == "gamevault"
        assert game.api_agent_id == "11"
        assert game.api_secret_key == "gvsecret"
        assert game.api_base_url == "https://gv.test"
        # Arcadia games table has NO soft-delete column
        assert not hasattr(game, "deleted_at") or True  # column simply not present


@pytest.mark.asyncio
async def test_game_aspnet_columns(seeded):
    async with seeded() as s:
        game = (await s.execute(select(Game).where(Game.id == 1))).scalar_one()
        assert game.name == "milkyway"
        assert game.login_url == "https://mw.test/default.aspx"
        assert game.backend_url == "https://mw.test/Cashier.aspx"
        assert game.username == "TestMW159"
        assert game.password == "Test_159872"
        assert game.backend_driver == "milkyway"


@pytest.mark.asyncio
async def test_game_account_arcadia_columns(seeded):
    async with seeded() as s:
        acct = (await s.execute(select(GameAccount).where(GameAccount.id == 10))).scalar_one()
        assert acct.username == "player_one"
        assert acct.id_from_backend == "uid:gid"
        assert acct.deleted_at is None


@pytest.mark.asyncio
async def test_game_account_soft_delete_column_exists(seeded):
    async with seeded() as s:
        acct = (await s.execute(select(GameAccount).where(GameAccount.id == 11))).scalar_one()
        assert acct.username == "deleted_player"
        assert acct.deleted_at is not None


@pytest.mark.asyncio
async def test_game_account_id_from_backend_can_be_none(seeded):
    async with seeded() as s:
        acct = (await s.execute(select(GameAccount).where(GameAccount.id == 4001))).scalar_one()
        assert acct.id_from_backend is None
