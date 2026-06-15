# app/db/models.py
from datetime import datetime

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    active: Mapped[bool] = mapped_column(default=True)
    login_url: Mapped[str | None] = mapped_column(default=None)
    backend_url: Mapped[str | None] = mapped_column(default=None)
    game_url: Mapped[str | None] = mapped_column(default=None)
    username: Mapped[str | None] = mapped_column(default=None)
    password: Mapped[str | None] = mapped_column(default=None)
    backend_driver: Mapped[str | None] = mapped_column(default=None)
    api_base_url: Mapped[str | None] = mapped_column(default=None)
    api_agent_id: Mapped[str | None] = mapped_column(default=None)
    api_secret_key: Mapped[str | None] = mapped_column(default=None)
    binding_key: Mapped[str | None] = mapped_column(default=None)
    # NOTE: Arcadia's games table has NO soft-delete column.


class GameAccount(Base):
    __tablename__ = "game_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int]
    game_id: Mapped[int]
    username: Mapped[str]
    password: Mapped[str]
    id_from_backend: Mapped[str | None] = mapped_column(default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(default=None)
