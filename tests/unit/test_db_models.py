# tests/unit/test_db_models.py
from sqlalchemy import select

from app.db.models import Game, GameAccount, GameOperation


async def test_models_map_and_query(seeded):
    async with seeded() as s:
        game = (await s.execute(select(Game).where(Game.id == 7))).scalar_one()
        assert game.api_agent_id == "agent-1"
        assert game.deleted_at is None
        acct = (await s.execute(select(GameAccount).where(GameAccount.id == 1001))).scalar_one()
        assert acct.username == "plyr_42"
    assert GameOperation.__tablename__ == "game_operations"
