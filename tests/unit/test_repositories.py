import pytest

from app.db.repositories import GameAccountsRepository, GamesRepository


@pytest.mark.asyncio
async def test_get_by_name_returns_game(seeded):
    async with seeded() as s:
        game = await GamesRepository(s).get_by_name("milkyway")
    assert game is not None and game.backend_driver == "milkyway"


@pytest.mark.asyncio
async def test_get_by_name_missing_returns_none(seeded):
    async with seeded() as s:
        assert await GamesRepository(s).get_by_name("nope") is None


@pytest.mark.asyncio
async def test_get_driver_by_name(seeded):
    async with seeded() as s:
        assert await GamesRepository(s).get_driver_by_name("milkyway") == "milkyway"


@pytest.mark.asyncio
async def test_get_account_by_username(seeded):
    async with seeded() as s:
        acct = await GameAccountsRepository(s).get_by_username(1, "player_one")
    assert acct is not None and acct.id_from_backend == "uid:gid"


@pytest.mark.asyncio
async def test_get_account_by_username_soft_deleted_is_hidden(seeded):
    async with seeded() as s:
        assert await GameAccountsRepository(s).get_by_username(1, "deleted_player") is None
