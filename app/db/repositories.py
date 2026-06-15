# app/db/repositories.py
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Game, GameAccount


class GamesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_name(self, name: str) -> Game | None:
        stmt = select(Game).where(Game.name == name)
        return (await self.session.execute(stmt)).scalars().first()

    async def get_driver_by_name(self, name: str) -> str | None:
        stmt = select(Game.backend_driver).where(Game.name == name)
        return (await self.session.execute(stmt)).scalars().first()


class GameAccountsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_username(self, game_id: int, username: str) -> GameAccount | None:
        stmt = select(GameAccount).where(
            GameAccount.game_id == game_id,
            GameAccount.username == username,
            GameAccount.deleted_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalars().first()
