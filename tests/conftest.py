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
        # milkyway (ASP.NET cashier) — game_id 1
        s.add(Game(
            id=1, name="milkyway", active=True, backend_driver="milkyway",
            login_url="https://mw.test/default.aspx",
            backend_url="https://mw.test/Cashier.aspx",
            game_url="https://mw.test/", username="TestMW159", password="Test_159872",
        ))
        s.add(GameAccount(
            id=10, user_id=42, game_id=1, username="player_one",
            password="acct-pw", id_from_backend="uid:gid",
        ))
        s.add(GameAccount(
            id=11, user_id=42, game_id=1, username="deleted_player",
            password="x", id_from_backend=None,
            deleted_at=__import__("datetime").datetime(2026, 1, 1),
        ))
        # gamevault (HTTP API) — game_id 9
        s.add(Game(
            id=9, name="GameVault Demo", active=True, backend_driver="gamevault",
            api_base_url="https://gv.test", api_agent_id="11", api_secret_key="gvsecret",
        ))
        s.add(Game(id=10_0, name="GameVault NoCreds", active=True, backend_driver="gamevault"))
        s.add(GameAccount(
            id=2001, user_id=43, game_id=9, username="user020301",
            password="x", id_from_backend="88880212",
        ))
        s.add(GameAccount(
            id=2002, user_id=44, game_id=9, username="user_no_ext",
            password="x", id_from_backend=None,
        ))
        # gameroom — game_id 11
        s.add(Game(
            id=11, name="Gameroom", active=True, backend_driver="gameroom",
            backend_url="https://gr.test", username="TestGR159", password="TestGR1122@",
        ))
        s.add(Game(id=12, name="Gameroom NoCreds", active=True, backend_driver="gameroom"))
        s.add(GameAccount(
            id=3001, user_id=51, game_id=11, username="apifull9983654",
            password="x", id_from_backend="2998032",
        ))
        s.add(GameAccount(
            id=3002, user_id=52, game_id=11, username="user_no_ext",
            password="x", id_from_backend=None,
        ))
        # goldentreasure — game_id 13
        s.add(Game(
            id=13, name="Golden Treasure", active=True, backend_driver="goldentreasure",
            backend_url="https://gt.test", username="Test02Gd1WEB", password="Zaeem@1233",
        ))
        s.add(Game(id=14, name="GT NoCreds", active=True, backend_driver="goldentreasure"))
        s.add(GameAccount(
            id=4001, user_id=61, game_id=13, username="apitest01",
            password="x", id_from_backend=None,
        ))
        await s.commit()
    return session_factory


@pytest_asyncio.fixture
async def fake_redis():
    """In-process fake Redis (full SET NX + TTL semantics) for session-store tests."""
    import fakeredis.aioredis as _f
    r = _f.FakeRedis(decode_responses=False)
    try:
        yield r
    finally:
        await r.aclose()


class FakeCaptchaSolver:
    """Reusable test double for CaptchaSolver. Returns canned answers or raises on demand.

    Default behavior: returns a fixed 5-digit string. Tests may construct with `answers=[...]`
    to return different solutions per call, or `raise_exc=...` to simulate solver failure.
    """

    def __init__(
        self, *, answers: list[str] | None = None, raise_exc: Exception | None = None
    ) -> None:
        self._answers = list(answers) if answers else ["34596"]
        self._raise = raise_exc
        self.calls: list[bytes] = []

    async def solve_numeric_image(self, image: bytes) -> str:
        self.calls.append(image)
        if self._raise is not None:
            raise self._raise
        if not self._answers:
            return "00000"
        return self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]


@pytest_asyncio.fixture
async def fake_captcha():
    return FakeCaptchaSolver()
