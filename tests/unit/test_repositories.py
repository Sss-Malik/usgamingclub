# tests/unit/test_repositories.py
from datetime import datetime

from app.db.models import Game
from app.db.repositories import (
    GameAccountsRepository,
    GameOperationsRepository,
    GamesRepository,
)


async def test_games_repo_get(seeded):
    async with seeded() as s:
        game = await GamesRepository(s).get(7)
        assert game is not None and game.api_agent_id == "agent-1"
        assert await GamesRepository(s).get(999) is None


async def test_games_repo_skips_soft_deleted(seeded):
    async with seeded() as s:
        s.add(Game(id=8, name="Deleted", active=True, deleted_at=datetime(2026, 1, 1)))
        await s.commit()
        assert await GamesRepository(s).get(8) is None


async def test_accounts_repo_get(seeded):
    async with seeded() as s:
        acct = await GameAccountsRepository(s).get(1001)
        assert acct is not None and acct.username == "plyr_42"
        assert await GameAccountsRepository(s).get(999) is None


async def test_operations_repo_get_by_key_returns_none_when_absent(seeded):
    async with seeded() as s:
        assert await GameOperationsRepository(s).get_by_idempotency_key("missing") is None


async def test_games_repo_get_driver_returns_string(seeded):
    async with seeded() as s:
        assert await GamesRepository(s).get_driver(11) == "gameroom"


async def test_games_repo_get_driver_returns_none_when_absent(seeded):
    async with seeded() as s:
        assert await GamesRepository(s).get_driver(99999) is None
