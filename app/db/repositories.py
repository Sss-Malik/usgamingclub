# app/db/repositories.py
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Game, GameAccount, GameOperation


class GamesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, game_id: int) -> Game | None:
        stmt = select(Game).where(Game.id == game_id, Game.deleted_at.is_(None))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_driver(self, game_id: int) -> str | None:
        """Read just the backend_driver column (cheap; used by the API endpoint for per-driver retry policy)."""
        stmt = select(Game.backend_driver).where(Game.id == game_id, Game.deleted_at.is_(None))
        return (await self.session.execute(stmt)).scalar_one_or_none()


class GameAccountsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, game_account_id: int) -> GameAccount | None:
        stmt = select(GameAccount).where(
            GameAccount.id == game_account_id, GameAccount.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


class GameOperationsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_idempotency_key(self, key: str) -> GameOperation | None:
        stmt = select(GameOperation).where(GameOperation.idempotency_key == key)
        return (await self.session.execute(stmt)).scalar_one_or_none()
