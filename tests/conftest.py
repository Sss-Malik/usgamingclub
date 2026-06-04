# tests/conftest.py
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.models import Base, Game, GameAccount


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture
async def seeded(session_factory):
    async with session_factory() as s:
        s.add(
            Game(
                id=7,
                name="Demo Game",
                active=True,
                api_base_url="https://api.example.test",
                api_agent_id="agent-1",
                api_secret_key="secret-1",
                binding_key="bind-1",
            )
        )
        s.add(
            GameAccount(
                id=1001,
                user_id=42,
                game_id=7,
                username="plyr_42",
                password="acct-pw",
                external_user_id="EXT-42",
            )
        )
        await s.commit()
    return session_factory
