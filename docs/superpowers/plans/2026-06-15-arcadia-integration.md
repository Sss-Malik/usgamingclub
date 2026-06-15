# Arcadia Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch this worker service's integration boundary from the original `casino-app`
to the Arcadia Laravel app (6 REST endpoints, two-secret HMAC, dollar-native money, name/
username lookups, Arcadia-shaped webhook), keeping the backend transport and money-safety
machinery intact.

**Architecture:** Translate-at-the-edge. Rewrite only the boundary (API, HMAC, schemas,
preflight, webhook, DB models, config) and make the money path dollar-native (delete the
internal cents conversions). The executor/result-cache/retry-blocked flow and the backend
clients/sessions/crypto are unchanged.

**Tech Stack:** Python 3.11+, FastAPI, arq (Redis), SQLAlchemy async, pydantic v2,
httpx, structlog, pytest/pytest-asyncio/respx/fakeredis.

**Spec:** `docs/superpowers/specs/2026-06-15-arcadia-integration-design.md`

---

## File Structure

**Create:**
- `app/api/automation.py` — 6 Arcadia REST endpoints + shared enqueue helper (replaces `operations.py`)
- `app/schemas/requests.py` — per-action inbound request models + internal `Operation` model (replaces `operations.py`)
- `app/backends/usernames.py` — username generator for `/create`
- `app/webhook/payload.py` — Arcadia webhook envelope builder
- `tests/unit/test_schemas_requests.py`, `tests/unit/test_usernames.py`, `tests/unit/test_webhook_payload.py`, `tests/integration/test_automation_endpoints.py`

**Modify:**
- `app/security/hmac.py`, `app/api/deps.py`, `app/config.py`, `.env.example`
- `app/db/models.py`, `app/db/repositories.py`, `app/preflight/checks.py`, `tests/conftest.py`
- `app/schemas/results.py`, `app/backends/base.py`, all 7 backend modules under `app/backends/*/backend.py`
- `app/operations/dispatch.py`, `app/operations/executor.py`, `app/operations/result_cache.py`
- `app/webhook/client.py`, `app/worker/tasks.py`, `app/main.py`, `app/logging.py`
- Many existing unit/integration tests (enumerated per task)

**Delete:**
- `app/api/operations.py`, `app/schemas/operations.py`
- `tests/unit/test_schemas_operations.py`, `tests/integration/test_operations_endpoint.py`
- `GameOperation` model + `GameOperationsRepository` (dead code)

---

## Phase 0 — Branch

### Task 0: Create the feature branch

- [ ] **Step 1: Branch from main**

```bash
cd /Applications/development/python/usgamingclub
git checkout -b feat/arcadia-integration
```

- [ ] **Step 2: Commit the approved spec**

```bash
git add docs/superpowers/specs/2026-06-15-arcadia-integration-design.md docs/superpowers/plans/2026-06-15-arcadia-integration.md
git commit -m "docs(arcadia): integration design + implementation plan"
```

---

## Phase 1 — Dollar-native backends

Self-contained: backend modules + their unit tests stay green throughout. Verify with
`make test` scoped to `tests/unit` after each task.

### Task 1: Dollar-native result models

**Files:**
- Modify: `app/schemas/results.py`
- Test: `tests/unit/test_schemas_results.py`

- [ ] **Step 1: Replace `app/schemas/results.py` body**

```python
# app/schemas/results.py
from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Result(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CreateAccountResult(_Result):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    external_user_id: str | None = None

    @field_validator("external_user_id")
    @classmethod
    def _non_empty_if_present(cls, v: str | None) -> str | None:
        if v is not None and v == "":
            raise ValueError("external_user_id must be non-empty if present")
        return v


class ReadBalanceResult(_Result):
    balance: float = Field(ge=0)


class ResetPasswordResult(_Result):
    password: str = Field(min_length=1)


class RechargeResult(_Result):
    balance: float | None = Field(default=None, ge=0)


class RedeemResult(_Result):
    balance: float | None = Field(default=None, ge=0)


class AgentBalanceResult(_Result):
    agent_balance: float = Field(ge=0)
```

- [ ] **Step 2: Update `tests/unit/test_schemas_results.py`**

Replace every `balance_cents` with `balance`, every `agent_balance_cents` with
`agent_balance`, and use dollar values (e.g. `ReadBalanceResult(balance=127.5)`).

- [ ] **Step 3: Run**

Run: `make test ARGS="tests/unit/test_schemas_results.py"` (or `pytest tests/unit/test_schemas_results.py -v`)
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/schemas/results.py tests/unit/test_schemas_results.py
git commit -m "refactor(results): dollar-native result models"
```

### Task 2: Dollar-native backend protocol

**Files:**
- Modify: `app/backends/base.py`
- Test: `tests/unit/test_backend_base.py`

- [ ] **Step 1: Replace the protocol methods in `app/backends/base.py`**

Replace the `recharge`/`redeem` signatures (lines 37-41) with:

```python
    async def recharge(self, ctx: BackendContext, *, amount: int) -> RechargeResult: ...

    async def redeem(self, ctx: BackendContext, *, amount: int) -> RedeemResult: ...
```

(Leave `create_account`, `read_balance`, `reset_password`, `agent_balance` as-is.)

- [ ] **Step 2: Update `tests/unit/test_backend_base.py`**

Any conformance/signature assertions referencing `amount_cents`/`bonus_cents`/
`total_credit_cents` become `amount`. Any result construction uses `balance=`/`agent_balance=`.

- [ ] **Step 3: Run**

Run: `pytest tests/unit/test_backend_base.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/backends/base.py tests/unit/test_backend_base.py
git commit -m "refactor(backends): dollar-native protocol (single amount)"
```

### Task 3: MockBackend dollar-native

**Files:**
- Modify: `app/backends/mock/backend.py`
- Test: `tests/unit/test_mock_backend.py`

- [ ] **Step 1: Replace the money methods (lines 34-54)**

```python
    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        self._maybe_fail()
        return ReadBalanceResult(balance=127.5)

    async def reset_password(self, ctx: BackendContext) -> ResetPasswordResult:
        self._maybe_fail()
        return ResetPasswordResult(password="MockReset123!")

    async def recharge(self, ctx: BackendContext, *, amount: int) -> RechargeResult:
        self._maybe_fail()
        return RechargeResult(balance=float(amount))

    async def redeem(self, ctx: BackendContext, *, amount: int) -> RedeemResult:
        self._maybe_fail()
        return RedeemResult(balance=0.0)

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult:
        self._maybe_fail()
        return AgentBalanceResult(agent_balance=1000.0)
```

- [ ] **Step 2: Update `tests/unit/test_mock_backend.py`**

`recharge(ctx, amount=50)` (not `amount_cents=`/`total_credit_cents=`); assert
`result.balance == 50.0`; `read_balance` → `balance == 127.5`; agent → `agent_balance == 1000.0`.

- [ ] **Step 3: Run**

Run: `pytest tests/unit/test_mock_backend.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/backends/mock/backend.py tests/unit/test_mock_backend.py
git commit -m "refactor(mock): dollar-native"
```

### Task 4: GameVaultBackend dollar-native

**Files:**
- Modify: `app/backends/gamevault/backend.py`
- Test: `tests/unit/test_gamevault_backend.py`

- [ ] **Step 1: Replace the conversion helpers + money methods**

Delete `import math` and the `_to_cents`/`_to_cents_opt`/`_to_dollars` helpers (lines 2, 18-27).
Add a small dollar formatter and rewrite reads/recharge/redeem/agent_balance:

```python
def _to_dollars_str(value: int) -> str:
    return str(int(value))


def _balance(value) -> float:
    return float(value)


def _balance_opt(value) -> float | None:
    return None if value is None else float(value)
```

```python
    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        uid = await self._user_id(ctx)
        data = await self._client.call("/api/external/userBalance", {"user_id": uid})
        return ReadBalanceResult(balance=_balance(data["user_balance"]))
```

```python
    async def recharge(self, ctx: BackendContext, *, amount: int) -> RechargeResult:
        uid = await self._user_id(ctx)
        data = await self._client.call(
            "/api/external/recharge",
            {"user_id": uid, "amount": _to_dollars_str(amount), "order_id": ctx.idempotency_key},
        )
        return RechargeResult(balance=_balance_opt(data.get("user_balance")))

    async def redeem(self, ctx: BackendContext, *, amount: int) -> RedeemResult:
        uid = await self._user_id(ctx)
        data = await self._client.call(
            "/api/external/withdraw",
            {"user_id": uid, "amount": _to_dollars_str(amount), "order_id": ctx.idempotency_key},
        )
        return RedeemResult(balance=_balance_opt(data.get("user_balance")))

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult:
        data = await self._client.call("/api/external/agentBalance", {})
        return AgentBalanceResult(agent_balance=_balance(data["agent_balance"]))
```

> Note: GameVault still expects whole-dollar `amount` strings; Arcadia sends whole dollars,
> so the wire value is unchanged (`"50"`). Balances were decimal dollars from GameVault; we
> now return them as dollars directly instead of `*100`.

- [ ] **Step 2: Update `tests/unit/test_gamevault_backend.py`**

`recharge(ctx, amount=50)`; assert the request `amount` field is `"50"`; balance assertions
use dollars (e.g. a GameVault `user_balance` of `127.5` → `result.balance == 127.5`, not
`12750`). Agent balance dollars likewise.

- [ ] **Step 3: Run**

Run: `pytest tests/unit/test_gamevault_backend.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/backends/gamevault/backend.py tests/unit/test_gamevault_backend.py
git commit -m "refactor(gamevault): dollar-native"
```

### Task 5: GameroomBackend dollar-native

**Files:**
- Modify: `app/backends/gameroom/backend.py`
- Test: `tests/unit/test_gameroom_backend.py`

- [ ] **Step 1: Replace helpers + money methods**

Delete `import math` and `_to_cents`/`_to_cents_opt`/`_to_dollars` (lines 2, 21-30). Add:

```python
def _balance(value) -> float:
    return float(value)


def _balance_opt(value) -> float | None:
    return None if value is None else float(value)
```

Rewrite the four sites:
- `agent_balance`: `return AgentBalanceResult(agent_balance=_balance(value))`
- `read_balance`: `return ReadBalanceResult(balance=_balance(data.get("balance", 0)))`
- `recharge(self, ctx, *, amount: int)`: `"balance": str(int(amount))` and
  `return RechargeResult(balance=_balance_opt(data.get("total_balance")))`
- `redeem(self, ctx, *, amount: int)`: `"balance": str(int(amount))` and `return RedeemResult()`

- [ ] **Step 2: Update `tests/unit/test_gameroom_backend.py`** — `amount=` kwarg; wire
  `balance` field `"50"`; balances in dollars.

- [ ] **Step 3: Run**

Run: `pytest tests/unit/test_gameroom_backend.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/backends/gameroom/backend.py tests/unit/test_gameroom_backend.py
git commit -m "refactor(gameroom): dollar-native"
```

### Task 6: GoldenTreasureBackend dollar-native

**Files:**
- Modify: `app/backends/goldentreasure/backend.py`
- Test: `tests/unit/test_goldentreasure_backend.py`

- [ ] **Step 1: Replace helpers + money methods**

Delete `import math` and `_to_cents`/`_to_dollars` (lines 2, 18-23). Add:

```python
def _balance(value) -> float:
    return float(value)
```

- `agent_balance`: `return AgentBalanceResult(agent_balance=_balance(v))`
- `read_balance`: `return ReadBalanceResult(balance=_balance(data.get("curScore", 0)))`
- `recharge(self, ctx, *, amount: int)`: `"score": str(int(amount))`, keep `RechargeResult()`
- `redeem(self, ctx, *, amount: int)`: `"score": str(-int(amount))`, keep `RedeemResult()`

- [ ] **Step 2: Update `tests/unit/test_goldentreasure_backend.py`** — `amount=` kwarg;
  `score` wire `"50"` / `"-50"`; balances dollars.

- [ ] **Step 3: Run**

Run: `pytest tests/unit/test_goldentreasure_backend.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/backends/goldentreasure/backend.py tests/unit/test_goldentreasure_backend.py
git commit -m "refactor(goldentreasure): dollar-native"
```

### Task 7: OrionStars + MilkyWay dollar-native

**Files:**
- Modify: `app/backends/orionstars/backend.py`, `app/backends/milkyway/backend.py`
- Test: `tests/unit/test_orionstars_backend.py`, `tests/unit/test_milkyway_backend.py`

- [ ] **Step 1: `app/backends/orionstars/backend.py`**

Delete `import math` and `_to_cents`/`_to_dollars` (lines 1, 18-23). Replace usages:
- `agent_balance`: `return AgentBalanceResult(agent_balance=float(dollars))`
- `read_balance`: `return ReadBalanceResult(balance=float(credit))`
- `recharge(self, ctx, *, amount: int)`: `extra_fields={"txtAddGold": str(int(amount)), "txtReason": ""}`
- `redeem(self, ctx, *, amount: int)`: `extra_fields={"txtAddGold": str(int(amount)), "txtReason": ""}`

> `fetch_agent_balance_dollars()` already returns dollars; keep it. `_to_cents(credit)`
> parsed a dollar string — `float(credit)` is the dollar value.

- [ ] **Step 2: `app/backends/milkyway/backend.py`**

Change the import `from app.backends.orionstars.backend import OrionStarsBackend, _to_cents`
to `from app.backends.orionstars.backend import OrionStarsBackend`, and line 32:
`return ReadBalanceResult(balance=float(credit))`.

- [ ] **Step 3: Update both unit tests** — `amount=` kwarg; `txtAddGold` wire `"50"`;
  balances in dollars (e.g. credit string `"127.50"` → `balance == 127.5`).

- [ ] **Step 4: Run**

Run: `pytest tests/unit/test_orionstars_backend.py tests/unit/test_milkyway_backend.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/backends/orionstars/backend.py app/backends/milkyway/backend.py tests/unit/test_orionstars_backend.py tests/unit/test_milkyway_backend.py
git commit -m "refactor(aspnet-cashier): dollar-native"
```

### Task 8: UltraPandaBackend dollar-native

**Files:**
- Modify: `app/backends/ultrapanda/backend.py`
- Test: `tests/unit/test_ultrapanda_backend.py`

- [ ] **Step 1: Replace `_cents_to_score` + money methods**

Replace `_cents_to_score` (lines 16-18) with:

```python
def _score(amount: int) -> str:
    """Format a whole-dollar amount as a 2-decimal-place dollar string for `score`."""
    return f"{int(amount):.2f}"
```

- `agent_balance`: `return AgentBalanceResult(agent_balance=float(limit))`
- `read_balance`: `return ReadBalanceResult(balance=float(cur))`
- `recharge(self, ctx, *, amount: int)`: `"score": _score(amount)`
- `redeem(self, ctx, *, amount: int)`: `"score": f"-{_score(amount)}"`

- [ ] **Step 2: Update `tests/unit/test_ultrapanda_backend.py`** — `amount=` kwarg; `score`
  wire `"50.00"` / `"-50.00"`; balances dollars (`curScore "127.50"` → `127.5`).

- [ ] **Step 3: Run**

Run: `pytest tests/unit/test_ultrapanda_backend.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/backends/ultrapanda/backend.py tests/unit/test_ultrapanda_backend.py
git commit -m "refactor(ultrapanda): dollar-native"
```

---

## Phase 2 — DB models, repositories, preflight (Arcadia schema)

### Task 9: SQLAlchemy models → Arcadia schema

**Files:**
- Modify: `app/db/models.py`
- Test: `tests/unit/test_db_models.py`

- [ ] **Step 1: Replace `app/db/models.py`**

```python
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
```

(Drop the `GameOperation` class entirely.)

- [ ] **Step 2: Update `tests/unit/test_db_models.py`** — drop any `GameOperation` test;
  drop references to `backend_username`/`backend_password`/`login_page_url`/`external_user_id`/
  `balance_cents`/`games.deleted_at`. Add coverage for `backend_driver`, `api_*`,
  `id_from_backend`, and `game_accounts.deleted_at`.

- [ ] **Step 3: Run**

Run: `pytest tests/unit/test_db_models.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/db/models.py tests/unit/test_db_models.py
git commit -m "refactor(db): models match Arcadia schema; drop GameOperation"
```

### Task 10: Repositories → name/username lookups

**Files:**
- Modify: `app/db/repositories.py`
- Test: `tests/unit/test_repositories.py`

- [ ] **Step 1: Replace `app/db/repositories.py`**

```python
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
```

> Game lookup uses `.first()` (no unique constraint on `name` is guaranteed); pick the first
> active-or-not row by name. (`active` is not filtered — Arcadia decides whether to send.)

- [ ] **Step 2: Rewrite `tests/unit/test_repositories.py`**

```python
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
```

(The `seeded` fixture is defined in Task 11.)

- [ ] **Step 3: Run** (after Task 11 lands the fixture)

Run: `pytest tests/unit/test_repositories.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/db/repositories.py tests/unit/test_repositories.py
git commit -m "refactor(db): name/username repositories; drop GameOperationsRepository"
```

### Task 11: Update `tests/conftest.py` seed data to Arcadia schema

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: Replace the `seeded` fixture body**

Replace the whole `seeded` fixture (the `Game(...)`/`GameAccount(...)` block) with the
Arcadia-schema columns. Cover one game per driver family plus a no-creds variant:

```python
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
```

> Map of old→new column names used in any other fixture/test: `backend_username`→`username`,
> `backend_password`→`password`, `login_page_url`→`login_url`, `external_user_id`→
> `id_from_backend`. `games` no longer has `deleted_at`; `game_accounts` keeps it.
> (`id=10_0` avoids colliding with `GameAccount id=10`; ids are independent across tables but
> kept distinct for readability.)

- [ ] **Step 2: Run the whole unit suite to surface fixture breakage**

Run: `pytest tests/unit -q`
Expected: failures only in tests not yet migrated (preflight/registry) — repositories/db pass.

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test(conftest): Arcadia-schema seed data"
```

### Task 12: Preflight → resolve by name/username

**Files:**
- Modify: `app/preflight/checks.py`
- Test: `tests/unit/test_preflight.py`

- [ ] **Step 1: Replace `app/preflight/checks.py`**

```python
from sqlalchemy.ext.asyncio import AsyncSession

from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.db.repositories import GameAccountsRepository, GamesRepository

ACCOUNT_SCOPED_TYPES = {"READ_BALANCE", "RESET_PASSWORD", "RECHARGE", "REDEEM", "FREEPLAY"}

_GAMEVAULT_DRIVERS = {"gamevault", "juwa", "juwa2"}


class PreflightError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


async def build_context(
    session: AsyncSession,
    *,
    type: str,
    backend_name: str,
    username: str | None,
    user_id: int | None,
    idempotency_key: str = "",
    account_username: str | None = None,
) -> BackendContext:
    game = await GamesRepository(session).get_by_name(backend_name)
    if game is None:
        raise PreflightError(f"game_not_found: {backend_name}")

    credentials = GameCredentials(
        game_id=game.id,
        name=game.name,
        backend_url=game.backend_url,
        login_page_url=game.login_url,
        backend_username=game.username,
        backend_password=game.password,
        api_base_url=game.api_base_url,
        api_agent_id=game.api_agent_id,
        api_secret_key=game.api_secret_key,
        binding_key=game.binding_key,
        backend_driver=game.backend_driver,
    )

    driver = (game.backend_driver or "").lower()
    if driver in _GAMEVAULT_DRIVERS and not (
        game.api_base_url and game.api_agent_id and game.api_secret_key
    ):
        raise PreflightError(f"missing_{driver}_credentials")
    if driver in {"gameroom", "goldentreasure", "milkyway", "firekirin", "pandamaster",
                  "orionstars", "ultrapanda", "vblink"} and not (
        game.backend_url and game.username and game.password
    ):
        raise PreflightError(f"missing_{driver}_credentials")

    account: AccountIdentity | None = None
    if type in ACCOUNT_SCOPED_TYPES:
        if not username:
            raise PreflightError("missing_username")
        acct = await GameAccountsRepository(session).get_by_username(game.id, username)
        if acct is None:
            raise PreflightError(f"game_account_not_found: {username}")
        account = AccountIdentity(
            game_account_id=acct.id,
            user_id=acct.user_id,
            game_id=acct.game_id,
            username=acct.username,
            external_user_id=acct.id_from_backend,
        )

    return BackendContext(
        credentials=credentials,
        user_id=user_id,
        account=account,
        idempotency_key=idempotency_key,
        account_username=account_username,
    )
```

- [ ] **Step 2: Rewrite `tests/unit/test_preflight.py`** to call `build_context` with
  `backend_name=`/`username=` (not `game_id=`/`game_account_id=`). Key cases:
  - game resolved by name → credentials mapped (`login_page_url == game.login_url`,
    `backend_username == game.username`).
  - `RECHARGE` with unknown username → `PreflightError("game_account_not_found: ...")`.
  - `RECHARGE` with no username → `PreflightError("missing_username")`.
  - missing creds per driver → `missing_<driver>_credentials`.
  - `CREATE_ACCOUNT` resolves no account (account is None).
  - `account.external_user_id == game_accounts.id_from_backend`.

- [ ] **Step 3: Run**

Run: `pytest tests/unit/test_preflight.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/preflight/checks.py tests/unit/test_preflight.py
git commit -m "refactor(preflight): resolve game by name, account by username"
```

---

## Phase 3 — HMAC, config, deps

### Task 13: Two-secret, two-scheme HMAC

**Files:**
- Modify: `app/security/hmac.py`
- Test: `tests/unit/test_hmac.py` (rewrite)

- [ ] **Step 1: Replace `app/security/hmac.py`**

```python
# app/security/hmac.py
import hashlib
import hmac
import time


def _hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def request_signature(secret: str, timestamp: str, raw_body: str | bytes) -> str:
    """Inbound scheme (Arcadia GameHttpService): HMAC over "{timestamp}.{body}", plain hex."""
    body = raw_body if isinstance(raw_body, (bytes, bytearray)) else raw_body.encode()
    return _hex(secret, f"{timestamp}.".encode() + body)


def verify_request(
    secret: str,
    timestamp: str,
    signature: str,
    raw_body: str | bytes,
    *,
    replay_window: int = 300,
    now: int | None = None,
) -> bool:
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = now if now is not None else int(time.time())
    if abs(current - ts) > replay_window:
        return False
    return hmac.compare_digest(request_signature(secret, timestamp, raw_body), signature)


def webhook_signature(secret: str, raw_body: str | bytes) -> str:
    """Outbound scheme (Arcadia AutomationWebhookController): HMAC over the raw body, plain hex."""
    body = raw_body if isinstance(raw_body, (bytes, bytearray)) else raw_body.encode()
    return _hex(secret, body)


def sign_webhook(secret: str, raw_body: str | bytes) -> dict[str, str]:
    return {
        "X-Webhook-Signature": webhook_signature(secret, raw_body),
        "Content-Type": "application/json",
    }
```

- [ ] **Step 2: Replace `tests/unit/test_hmac.py`**

```python
import hashlib
import hmac as _hmac

from app.security.hmac import (
    request_signature,
    sign_webhook,
    verify_request,
    webhook_signature,
)

SECRET = "shared-secret"
BODY = '{"user_id":1,"backend_name":"milkyway"}'


def test_request_signature_matches_php_reference():
    ts = "1733345678"
    expected = _hmac.new(SECRET.encode(), f"{ts}.{BODY}".encode(), hashlib.sha256).hexdigest()
    assert request_signature(SECRET, ts, BODY) == expected  # plain hex, no prefix


def test_verify_request_roundtrip():
    ts = "1000"
    sig = request_signature(SECRET, ts, BODY)
    assert verify_request(SECRET, ts, sig, BODY, now=1000)


def test_verify_request_rejects_tamper_and_stale_and_missing():
    ts = "1000"
    sig = request_signature(SECRET, ts, BODY)
    assert not verify_request(SECRET, ts, sig, BODY + "x", now=1000)
    assert not verify_request(SECRET, ts, sig, BODY, now=1000 + 301)
    assert not verify_request(SECRET, "nan", sig, BODY, now=1000)
    assert not verify_request(SECRET, ts, "", BODY, now=1000)
    assert not verify_request("", ts, sig, BODY, now=1000)


def test_webhook_signature_matches_php_reference():
    expected = _hmac.new(SECRET.encode(), BODY.encode(), hashlib.sha256).hexdigest()
    assert webhook_signature(SECRET, BODY) == expected
    headers = sign_webhook(SECRET, BODY)
    assert headers["X-Webhook-Signature"] == expected
    assert headers["Content-Type"] == "application/json"


def test_signatures_work_over_bytes():
    assert webhook_signature(SECRET, BODY.encode()) == webhook_signature(SECRET, BODY)
```

- [ ] **Step 3: Run**

Run: `pytest tests/unit/test_hmac.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/security/hmac.py tests/unit/test_hmac.py
git commit -m "feat(hmac): two-secret inbound/outbound schemes (Arcadia)"
```

### Task 14: Config — two secrets + Arcadia webhook path

**Files:**
- Modify: `app/config.py`
- Test: `tests/unit/test_config.py`

- [ ] **Step 1: Edit `app/config.py`**

Replace `python_signing_secret: str = ""` with:

```python
    api_secret: str = ""          # inbound request HMAC (Arcadia AUTOMATION_API_SECRET)
    webhook_secret: str = ""      # outbound webhook HMAC (Arcadia AUTOMATION_WEBHOOK_SECRET)
```

Replace the `webhook_url`/`ping_url` properties with:

```python
    @property
    def webhook_url(self) -> str:
        return f"{self.app_url.rstrip('/')}/api/automation/webhook"
```

(Remove `ping_url` if nothing references it; otherwise repoint as needed.)

Replace `require_runtime_settings` body:

```python
def require_runtime_settings(settings: Settings) -> None:
    missing = [
        name for name, val in (("API_SECRET", settings.api_secret),
                               ("WEBHOOK_SECRET", settings.webhook_secret)) if not val
    ]
    if missing:
        raise RuntimeError(
            f"{', '.join(missing)} not set — inbound requests and outbound webhooks "
            "cannot be authenticated. Configure them to match Arcadia."
        )
```

- [ ] **Step 2: Update `tests/unit/test_config.py`** — set `api_secret`/`webhook_secret`;
  assert `webhook_url` ends with `/api/automation/webhook`; assert `require_runtime_settings`
  raises when either secret is empty and passes when both set. Drop `ping_url`/
  `python_signing_secret` assertions.

- [ ] **Step 3: Run**

Run: `pytest tests/unit/test_config.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/config.py tests/unit/test_config.py
git commit -m "feat(config): api_secret + webhook_secret; Arcadia webhook path"
```

### Task 15: Inbound signature dependency

**Files:**
- Modify: `app/api/deps.py`
- Test: covered by Task 19's endpoint tests.

- [ ] **Step 1: Replace `app/api/deps.py`**

```python
# app/api/deps.py
from fastapi import HTTPException, Request

from app.config import get_settings
from app.security.hmac import verify_request


async def verify_request_signature(request: Request) -> bytes:
    raw = await request.body()
    settings = get_settings()
    ok = verify_request(
        settings.api_secret,
        request.headers.get("X-Request-Timestamp", ""),
        request.headers.get("X-Request-Signature", ""),
        raw,
        replay_window=settings.replay_window_seconds,
    )
    if not ok:
        raise HTTPException(status_code=401, detail="Signature invalid")
    return raw
```

- [ ] **Step 2: Commit**

```bash
git add app/api/deps.py
git commit -m "feat(api): inbound X-Request-Signature dependency"
```

---

## Phase 4 — Request schemas, internal op, username generator

### Task 16: Inbound request schemas + internal `Operation`

**Files:**
- Create: `app/schemas/requests.py`
- Delete: `app/schemas/operations.py`
- Create test: `tests/unit/test_schemas_requests.py`
- Delete test: `tests/unit/test_schemas_operations.py`

- [ ] **Step 1: Write `tests/unit/test_schemas_requests.py`**

```python
import pytest
from pydantic import ValidationError

from app.schemas.requests import (
    CreateRequest,
    FreeplayRequest,
    ReadRequest,
    RechargeRequest,
    ResetPasswordRequest,
    WithdrawRequest,
    Operation,
)


def test_recharge_request_valid():
    r = RechargeRequest.model_validate({
        "user_id": 1, "backend_name": "milkyway", "username": "p1",
        "amount": 50, "transaction_id": "uuid-1",
    })
    assert r.amount == 50 and r.transaction_id == "uuid-1"


def test_recharge_request_rejects_missing_fields():
    with pytest.raises(ValidationError):
        RechargeRequest.model_validate({"user_id": 1, "backend_name": "x"})


def test_create_request_needs_full_name():
    r = CreateRequest.model_validate({"user_id": 1, "full_name": "John Doe", "backend_name": "mw"})
    assert r.full_name == "John Doe"


def test_operation_roundtrips_via_dict():
    op = Operation.model_validate({
        "action": "recharge", "type": "RECHARGE", "idempotency_key": "recharge:uuid-1",
        "user_id": 1, "backend_name": "milkyway", "username": "p1", "amount": 50,
        "correlation": {"transaction_id": "uuid-1"},
    })
    assert op.action == "recharge" and op.correlation["transaction_id"] == "uuid-1"
```

- [ ] **Step 2: Write `app/schemas/requests.py`**

```python
# app/schemas/requests.py
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: int
    backend_name: str = Field(min_length=1)


class CreateRequest(_In):
    full_name: str = Field(min_length=1)


class RechargeRequest(_In):
    username: str = Field(min_length=1)
    amount: int = Field(ge=0)
    transaction_id: str = Field(min_length=1)


class WithdrawRequest(_In):
    username: str = Field(min_length=1)
    amount: int = Field(ge=0)
    redeem_id: int


class ResetPasswordRequest(_In):
    username: str = Field(min_length=1)
    reset_password_id: int


class FreeplayRequest(_In):
    username: str = Field(min_length=1)
    amount: int = Field(ge=0)
    freeplay_id: int


class ReadRequest(_In):
    username: str = Field(min_length=1)
    read_id: int


class Operation(BaseModel):
    """Normalized internal op carried through arq → executor → webhook builder."""

    model_config = ConfigDict(extra="ignore")
    action: Literal["create", "recharge", "redeem", "reset_password", "freeplay", "read"]
    type: Literal[
        "CREATE_ACCOUNT", "RECHARGE", "REDEEM", "RESET_PASSWORD", "FREEPLAY", "READ_BALANCE"
    ]
    idempotency_key: str = Field(min_length=1)
    user_id: int
    backend_name: str = Field(min_length=1)
    username: str | None = None
    account_username: str | None = None
    amount: int | None = Field(default=None, ge=0)
    correlation: dict[str, str | int] = Field(default_factory=dict)
```

- [ ] **Step 3: Delete old files**

```bash
git rm app/schemas/operations.py tests/unit/test_schemas_operations.py
```

- [ ] **Step 4: Run**

Run: `pytest tests/unit/test_schemas_requests.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/schemas/requests.py tests/unit/test_schemas_requests.py
git commit -m "feat(schemas): Arcadia request models + internal Operation"
```

### Task 17: Username generator

**Files:**
- Create: `app/backends/usernames.py`
- Create test: `tests/unit/test_usernames.py`

- [ ] **Step 1: Write `tests/unit/test_usernames.py`**

```python
import re

from app.backends.usernames import generate_username


def test_derives_from_full_name_alnum_lowercase():
    u = generate_username("John O'Brien")
    assert re.fullmatch(r"[a-z]+[0-9]{4}", u)
    assert u.startswith("johnobrien")


def test_truncates_long_names():
    u = generate_username("A" * 50)
    # base capped at 12 chars + 4 digits
    assert len(u) <= 16
    assert re.fullmatch(r"a{1,12}[0-9]{4}", u)


def test_falls_back_when_no_alnum():
    u = generate_username("***")
    assert re.fullmatch(r"user[0-9]{4}", u)


def test_is_nondeterministic_suffix():
    assert generate_username("Jane Doe") != generate_username("Jane Doe")
```

- [ ] **Step 2: Write `app/backends/usernames.py`**

```python
# app/backends/usernames.py
import re
import secrets

_MAX_BASE = 12


def generate_username(full_name: str) -> str:
    """Provider-safe username derived from a display name.

    Lowercase alphanumerics from `full_name` (truncated), plus 4 random digits for
    uniqueness. Falls back to `user` when the name yields nothing usable.
    """
    base = re.sub(r"[^a-z0-9]", "", (full_name or "").lower())[:_MAX_BASE]
    if not base:
        base = "user"
    suffix = f"{secrets.randbelow(10000):04d}"
    return f"{base}{suffix}"
```

- [ ] **Step 3: Run**

Run: `pytest tests/unit/test_usernames.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/backends/usernames.py tests/unit/test_usernames.py
git commit -m "feat(backends): username generator for create"
```

---

## Phase 5 — API endpoints

### Task 18: Arcadia REST endpoints

**Files:**
- Create: `app/api/automation.py`
- Delete: `app/api/operations.py`
- Modify: `app/main.py`
- Create test: `tests/integration/test_automation_endpoints.py`
- Delete test: `tests/integration/test_operations_endpoint.py`

- [ ] **Step 1: Write `app/api/automation.py`**

```python
# app/api/automation.py
import json
import time

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import verify_request_signature
from app.backends.registry import NON_IDEMPOTENT_DRIVERS
from app.backends.usernames import generate_username
from app.db.repositories import GamesRepository
from app.logging import get_logger
from app.schemas.requests import (
    CreateRequest,
    FreeplayRequest,
    Operation,
    ReadRequest,
    RechargeRequest,
    ResetPasswordRequest,
    WithdrawRequest,
)

router = APIRouter()
logger = get_logger(__name__)


async def _enqueue(request: Request, op: Operation) -> Response:
    payload = op.model_dump()
    # Per-driver retry policy (non-idempotent drivers run at most once). Peek the driver by
    # game name; a DB blip falls back to the worker default (preflight surfaces real errors).
    try:
        session_factory = getattr(request.app.state, "session_factory", None)
        if session_factory is not None:
            async with session_factory() as session:
                driver = await GamesRepository(session).get_driver_by_name(op.backend_name)
            if driver and driver.lower() in NON_IDEMPOTENT_DRIVERS:
                payload = {**payload, "_max_tries": 1}
    except Exception:  # noqa: BLE001
        logger.exception("driver_peek_failed", idempotency_key=op.idempotency_key)

    try:
        await request.app.state.arq.enqueue_job(
            "execute_operation_task", payload, _job_id=op.idempotency_key,
        )
    except Exception:  # noqa: BLE001
        logger.exception("operation_enqueue_failed", idempotency_key=op.idempotency_key)
        return Response(status_code=500)
    logger.bind(idempotency_key=op.idempotency_key, action=op.action).info("operation_enqueued")
    return Response(status_code=202)


def _request_ts(request: Request) -> str:
    return request.headers.get("X-Request-Timestamp") or str(int(time.time()))


@router.post("/create")
async def create(request: Request, raw: bytes = Depends(verify_request_signature)) -> Response:
    req = CreateRequest.model_validate(json.loads(raw))
    op = Operation(
        action="create", type="CREATE_ACCOUNT",
        idempotency_key=f"create:{req.user_id}:{req.backend_name}:{_request_ts(request)}",
        user_id=req.user_id, backend_name=req.backend_name,
        account_username=generate_username(req.full_name),
    )
    return await _enqueue(request, op)


@router.post("/recharge")
async def recharge(request: Request, raw: bytes = Depends(verify_request_signature)) -> Response:
    req = RechargeRequest.model_validate(json.loads(raw))
    op = Operation(
        action="recharge", type="RECHARGE",
        idempotency_key=f"recharge:{req.transaction_id}",
        user_id=req.user_id, backend_name=req.backend_name, username=req.username,
        amount=req.amount, correlation={"transaction_id": req.transaction_id},
    )
    return await _enqueue(request, op)


@router.post("/withdraw")
async def withdraw(request: Request, raw: bytes = Depends(verify_request_signature)) -> Response:
    req = WithdrawRequest.model_validate(json.loads(raw))
    op = Operation(
        action="redeem", type="REDEEM",
        idempotency_key=f"redeem:{req.redeem_id}",
        user_id=req.user_id, backend_name=req.backend_name, username=req.username,
        amount=req.amount, correlation={"redeem_id": req.redeem_id},
    )
    return await _enqueue(request, op)


@router.post("/reset-password")
async def reset_password(request: Request, raw: bytes = Depends(verify_request_signature)) -> Response:
    req = ResetPasswordRequest.model_validate(json.loads(raw))
    op = Operation(
        action="reset_password", type="RESET_PASSWORD",
        idempotency_key=f"reset_password:{req.reset_password_id}",
        user_id=req.user_id, backend_name=req.backend_name, username=req.username,
        correlation={"reset_password_id": req.reset_password_id},
    )
    return await _enqueue(request, op)


@router.post("/freeplay")
async def freeplay(request: Request, raw: bytes = Depends(verify_request_signature)) -> Response:
    req = FreeplayRequest.model_validate(json.loads(raw))
    op = Operation(
        action="freeplay", type="FREEPLAY",
        idempotency_key=f"freeplay:{req.freeplay_id}",
        user_id=req.user_id, backend_name=req.backend_name, username=req.username,
        amount=req.amount, correlation={"freeplay_id": req.freeplay_id},
    )
    return await _enqueue(request, op)


@router.post("/read")
async def read(request: Request, raw: bytes = Depends(verify_request_signature)) -> Response:
    req = ReadRequest.model_validate(json.loads(raw))
    op = Operation(
        action="read", type="READ_BALANCE",
        idempotency_key=f"read:{req.read_id}",
        user_id=req.user_id, backend_name=req.backend_name, username=req.username,
        correlation={"read_id": req.read_id},
    )
    return await _enqueue(request, op)
```

> Validation errors raise `pydantic.ValidationError` → FastAPI returns `422`. Arcadia treats
> any non-2xx as a failed outbound call and relies on the webhook for the real result; a
> malformed body that never enqueues simply never produces a webhook (acceptable — Arcadia's
> timeout safety net resolves it). The bad-signature path returns `401` via the dependency.

- [ ] **Step 2: Update `app/main.py`**

Change the import `from app.api import health, operations` → `from app.api import automation, health`
and `app.include_router(operations.router)` → `app.include_router(automation.router)`.

- [ ] **Step 3: Write `tests/integration/test_automation_endpoints.py`**

```python
import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.security.hmac import request_signature


class FakeArq:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, func, payload, _job_id=None):
        self.jobs.append((func, payload, _job_id))


@pytest.fixture
def client(monkeypatch, session_factory):
    monkeypatch.setenv("API_SECRET", "in-secret")
    monkeypatch.setenv("WEBHOOK_SECRET", "out-secret")
    from app.config import get_settings
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides = {}
    fake = FakeArq()
    with TestClient(app) as c:
        c.app.state.arq = fake
        c.app.state.session_factory = session_factory
        c.fake = fake
        yield c


def _post(client, path, body):
    raw = json.dumps(body)
    ts = "1000"
    import app.config
    secret = app.config.get_settings().api_secret
    sig = request_signature(secret, ts, raw)
    return client.post(
        path, content=raw,
        headers={"X-Request-Timestamp": ts, "X-Request-Signature": sig,
                 "Content-Type": "application/json"},
    )


def test_recharge_enqueues_202(client):
    body = {"user_id": 42, "backend_name": "milkyway", "username": "player_one",
            "amount": 50, "transaction_id": "uuid-1"}
    resp = _post(client, "/recharge", body)
    assert resp.status_code == 202
    func, payload, job_id = client.fake.jobs[-1]
    assert func == "execute_operation_task" and job_id == "recharge:uuid-1"
    assert payload["type"] == "RECHARGE" and payload["amount"] == 50
    assert payload["correlation"] == {"transaction_id": "uuid-1"}
    # milkyway is non-idempotent → capped at 1 try
    assert payload["_max_tries"] == 1


def test_create_generates_username(client):
    resp = _post(client, "/create",
                 {"user_id": 7, "full_name": "Jane Doe", "backend_name": "milkyway"})
    assert resp.status_code == 202
    _f, payload, _j = client.fake.jobs[-1]
    assert payload["type"] == "CREATE_ACCOUNT" and payload["account_username"].startswith("janedoe")


def test_bad_signature_401(client):
    raw = json.dumps({"user_id": 1, "backend_name": "milkyway", "username": "p",
                      "amount": 5, "transaction_id": "t"})
    resp = client.post("/recharge", content=raw,
                       headers={"X-Request-Timestamp": "1000", "X-Request-Signature": "bad"})
    assert resp.status_code == 401
    assert client.fake.jobs == []


def test_invalid_body_422(client):
    resp = _post(client, "/recharge", {"user_id": 1, "backend_name": "milkyway"})
    assert resp.status_code == 422
```

> If the suite freshness window (300s) rejects `ts="1000"`, patch `verify_request`'s `now` by
> setting `X-Request-Timestamp` to a current value; simplest is to monkeypatch
> `app.api.deps.verify_request` to ignore the window in this test module, or set the timestamp
> dynamically via `str(int(time.time()))` and sign that. Use the dynamic-timestamp approach.

- [ ] **Step 4: Delete the old endpoint test + module**

```bash
git rm app/api/operations.py tests/integration/test_operations_endpoint.py
```

- [ ] **Step 5: Run**

Run: `pytest tests/integration/test_automation_endpoints.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/api/automation.py app/main.py tests/integration/test_automation_endpoints.py
git commit -m "feat(api): Arcadia REST endpoints replace /operations"
```

---

## Phase 6 — Dispatch, webhook payload, webhook client, executor, worker

### Task 19: Dispatch — dollar passthrough + freeplay→recharge

**Files:**
- Modify: `app/operations/dispatch.py`
- Test: `tests/unit/test_dispatch.py` (create if absent)

- [ ] **Step 1: Replace `app/operations/dispatch.py`**

```python
# app/operations/dispatch.py
from pydantic import BaseModel

from app.backends.base import BackendError, GameBackend
from app.backends.context import BackendContext


async def dispatch(backend: GameBackend, op, ctx: BackendContext) -> BaseModel:
    if op.type == "CREATE_ACCOUNT":
        return await backend.create_account(ctx)
    if op.type == "READ_BALANCE":
        return await backend.read_balance(ctx)
    if op.type == "RESET_PASSWORD":
        return await backend.reset_password(ctx)
    if op.type in ("RECHARGE", "FREEPLAY"):
        # Freeplay is an additive credit — same backend op as recharge.
        return await backend.recharge(ctx, amount=int(op.amount or 0))
    if op.type == "REDEEM":
        return await backend.redeem(ctx, amount=int(op.amount or 0))
    raise BackendError(f"unsupported_type: {op.type}")
```

- [ ] **Step 2: Write `tests/unit/test_dispatch.py`**

```python
import pytest

from app.operations.dispatch import dispatch
from app.schemas.requests import Operation


class _Spy:
    def __init__(self):
        self.calls = []

    async def recharge(self, ctx, *, amount):
        self.calls.append(("recharge", amount)); return _R()

    async def redeem(self, ctx, *, amount):
        self.calls.append(("redeem", amount)); return _R()

    async def read_balance(self, ctx):
        self.calls.append(("read", None)); return _R()


class _R:
    def model_dump(self, **k):
        return {}


def _op(type_, amount=None):
    return Operation(action="recharge", type=type_, idempotency_key="k",
                     user_id=1, backend_name="x", amount=amount)


@pytest.mark.asyncio
async def test_freeplay_maps_to_recharge():
    spy = _Spy()
    await dispatch(spy, _op("FREEPLAY", 50), ctx=None)
    assert spy.calls == [("recharge", 50)]


@pytest.mark.asyncio
async def test_recharge_passes_dollars():
    spy = _Spy()
    await dispatch(spy, _op("RECHARGE", 25), ctx=None)
    assert spy.calls == [("recharge", 25)]
```

- [ ] **Step 3: Run**

Run: `pytest tests/unit/test_dispatch.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/operations/dispatch.py tests/unit/test_dispatch.py
git commit -m "refactor(dispatch): dollar passthrough; freeplay->recharge"
```

### Task 20: Webhook payload builder

**Files:**
- Create: `app/webhook/payload.py`
- Create test: `tests/unit/test_webhook_payload.py`

- [ ] **Step 1: Write `tests/unit/test_webhook_payload.py`**

```python
from app.operations.result_cache import CachedOutcome
from app.schemas.requests import Operation
from app.webhook.payload import build_webhook_payload

GENERIC = "Something went wrong. Please try again later."


def _op(action, type_, **kw):
    base = dict(action=action, type=type_, idempotency_key="k", user_id=42,
                backend_name="milkyway")
    base.update(kw)
    return Operation(**base)


def test_recharge_success_echoes_amount_and_txn():
    op = _op("recharge", "RECHARGE", amount=50, correlation={"transaction_id": "uuid-1"})
    out = CachedOutcome("succeeded", {"balance": 1234.0}, None)
    body = build_webhook_payload(op, out, backend_id=1)
    assert body["action"] == "recharge" and body["status"] == "success"
    assert body["user_id"] == 42 and body["backend_id"] == 1 and body["backend_name"] == "milkyway"
    assert body["transaction_id"] == "uuid-1" and body["amount"] == 50
    assert isinstance(body["timestamp"], int)


def test_recharge_failure_keeps_txn_and_amount_with_message():
    op = _op("recharge", "RECHARGE", amount=50, correlation={"transaction_id": "uuid-1"})
    out = CachedOutcome("failed", None, "backend_error: Insufficient balance")
    body = build_webhook_payload(op, out, backend_id=1)
    assert body["status"] == "failed" and body["transaction_id"] == "uuid-1"
    assert body["amount"] == 50 and body["message"] == "Insufficient balance"


def test_error_status_generic_message():
    op = _op("recharge", "RECHARGE", amount=50, correlation={"transaction_id": "uuid-1"})
    out = CachedOutcome("error", None, "backend_error: unexpected")
    body = build_webhook_payload(op, out, backend_id=1)
    assert body["status"] == "error" and body["message"] == GENERIC


def test_create_success_account_created():
    op = _op("create", "CREATE_ACCOUNT", account_username="janedoe1234")
    out = CachedOutcome("succeeded",
                        {"username": "janedoe1234", "password": "p", "external_user_id": "u:g"}, None)
    body = build_webhook_payload(op, out, backend_id=1)
    assert body["account_created"] == [
        {"username": "janedoe1234", "password": "p", "id_from_backend": "u:g"}
    ]


def test_reset_password_success_new_password():
    op = _op("reset_password", "RESET_PASSWORD", correlation={"reset_password_id": 9})
    out = CachedOutcome("succeeded", {"password": "newpw"}, None)
    body = build_webhook_payload(op, out, backend_id=1)
    assert body["reset_password_id"] == 9 and body["new_password"] == "newpw"


def test_read_success_balance_dollars():
    op = _op("read", "READ_BALANCE", correlation={"read_id": 5})
    out = CachedOutcome("succeeded", {"balance": 127.5}, None)
    body = build_webhook_payload(op, out, backend_id=1)
    assert body["read_id"] == 5 and body["user_data"] == {"balance": 127.5}
```

- [ ] **Step 2: Write `app/webhook/payload.py`**

```python
# app/webhook/payload.py
import time

from app.operations.result_cache import CachedOutcome
from app.schemas.requests import Operation

GENERIC_MESSAGE = "Something went wrong. Please try again later."


def _status(outcome: CachedOutcome) -> str:
    return {"succeeded": "success", "failed": "failed", "error": "error"}.get(
        outcome.status, "error"
    )


def _message(outcome: CachedOutcome) -> str:
    if outcome.status == "succeeded":
        return ""
    if outcome.status == "error":
        return GENERIC_MESSAGE
    reason = outcome.reason or "failed"
    # Surface provider/business text without the internal prefix.
    for prefix in ("backend_error: ", "preflight_failed: ", "invalid_payload: ",
                   "invalid_result_payload: "):
        if reason.startswith(prefix):
            reason = reason[len(prefix):]
            break
    return reason or GENERIC_MESSAGE


def build_webhook_payload(
    op: Operation, outcome: CachedOutcome, *, backend_id: int | None
) -> dict:
    status = _status(outcome)
    body: dict = {
        "action": op.action,
        "status": status,
        "message": _message(outcome),
        "timestamp": int(time.time()),
        "user_id": op.user_id,
        "backend_name": op.backend_name,
    }
    if backend_id is not None:
        body["backend_id"] = backend_id

    # Correlation ids are always echoed so Arcadia can resolve the local row.
    body.update(op.correlation)

    # Money ops echo the original (whole-dollar) amount for Arcadia's amount-verification.
    if op.action in ("recharge", "redeem", "freeplay") and op.amount is not None:
        body["amount"] = op.amount

    if status != "success":
        return body

    result = outcome.result or {}
    if op.action == "create":
        body["account_created"] = [{
            "username": result.get("username"),
            "password": result.get("password"),
            "id_from_backend": result.get("external_user_id"),
        }]
    elif op.action == "reset_password":
        body["new_password"] = result.get("password")
    elif op.action == "read":
        body["user_data"] = {"balance": result.get("balance")}
    return body
```

- [ ] **Step 3: Run**

Run: `pytest tests/unit/test_webhook_payload.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/webhook/payload.py tests/unit/test_webhook_payload.py
git commit -m "feat(webhook): Arcadia envelope builder"
```

### Task 21: Webhook client — raw-body signing + per-attempt fresh timestamp

**Files:**
- Modify: `app/webhook/client.py`
- Test: `tests/unit/test_webhook_client.py` (rewrite)

- [ ] **Step 1: Replace `app/webhook/client.py`**

```python
import asyncio
import json
import random
import time
from dataclasses import dataclass

import httpx

from app.logging import get_logger
from app.security.hmac import sign_webhook

logger = get_logger(__name__)

# 403 = bad signature / stale / IP-blocked on the Arcadia side; retrying won't help.
NO_RETRY_STATUSES = {401, 403, 422}


@dataclass
class WebhookResult:
    delivered: bool
    status_code: int | None
    attempts: int


async def deliver_webhook(
    client: httpx.AsyncClient,
    url: str,
    secret: str,
    payload: dict,
    *,
    max_budget_seconds: float,
    backoff_base: float = 0.5,
    backoff_max: float = 30.0,
    now=time.monotonic,
    now_unix=lambda: int(time.time()),
    sleep=asyncio.sleep,
) -> WebhookResult:
    deadline = now() + max_budget_seconds
    attempt = 0
    last_status: int | None = None

    while True:
        attempt += 1
        # Refresh the in-body timestamp every attempt: Arcadia rejects webhooks whose
        # signed `data.timestamp` is >60s stale, and retries can span minutes.
        payload = {**payload, "timestamp": now_unix()}
        raw = json.dumps(payload, separators=(",", ":"))
        headers = sign_webhook(secret, raw)
        try:
            resp = await client.post(url, content=raw.encode(), headers=headers)
            last_status = resp.status_code
            if last_status == 200:
                logger.info("webhook_delivered", phase="webhook_delivered", attempts=attempt)
                return WebhookResult(True, 200, attempt)
            if last_status in NO_RETRY_STATUSES:
                logger.error("webhook_rejected", phase="webhook_attempt", status=last_status)
                return WebhookResult(False, last_status, attempt)
        except httpx.HTTPError as exc:
            last_status = None
            logger.warning("webhook_conn_error", phase="webhook_attempt", error=str(exc))

        delay = min(backoff_max, backoff_base * (2 ** (attempt - 1)))
        delay += random.uniform(0, delay * 0.25)
        if now() + delay >= deadline:
            logger.error("webhook_gave_up", phase="failed", attempts=attempt, status=last_status)
            return WebhookResult(False, last_status, attempt)
        await sleep(delay)
```

- [ ] **Step 2: Rewrite `tests/unit/test_webhook_client.py`**

```python
import json

import httpx
import respx

from app.security.hmac import webhook_signature
from app.webhook.client import deliver_webhook

URL = "https://arcadia.test/api/automation/webhook"
SECRET = "out-secret"
PAYLOAD = {"action": "recharge", "status": "success", "user_id": 1}


class FakeClock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


async def _noop_sleep(_s): return None


@respx.mock
async def test_delivers_on_200_signs_raw_body_with_fresh_timestamp():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"success": True}))
    async with httpx.AsyncClient() as client:
        res = await deliver_webhook(client, URL, SECRET, PAYLOAD,
                                    max_budget_seconds=600, now_unix=lambda: 1234)
    assert res.delivered and res.status_code == 200 and res.attempts == 1
    sent = route.calls.last.request
    body = sent.content.decode()
    assert json.loads(body)["timestamp"] == 1234
    assert sent.headers["X-Webhook-Signature"] == webhook_signature(SECRET, body)


@respx.mock
async def test_retries_on_500_then_succeeds_and_refreshes_timestamp():
    times = iter([100, 200])
    respx.post(URL).mock(side_effect=[httpx.Response(500), httpx.Response(200)])
    async with httpx.AsyncClient() as client:
        res = await deliver_webhook(client, URL, SECRET, PAYLOAD, max_budget_seconds=600,
                                    sleep=_noop_sleep, now_unix=lambda: next(times))
    assert res.delivered and res.attempts == 2


@respx.mock
async def test_does_not_retry_on_403():
    route = respx.post(URL).mock(return_value=httpx.Response(403))
    async with httpx.AsyncClient() as client:
        res = await deliver_webhook(client, URL, SECRET, PAYLOAD, max_budget_seconds=600,
                                    sleep=_noop_sleep)
    assert not res.delivered and res.status_code == 403 and route.call_count == 1


@respx.mock
async def test_gives_up_after_budget():
    respx.post(URL).mock(return_value=httpx.Response(500))
    clock = FakeClock()

    async def advancing_sleep(s): clock.t += s

    async with httpx.AsyncClient() as client:
        res = await deliver_webhook(client, URL, SECRET, PAYLOAD, max_budget_seconds=5,
                                    backoff_base=1, backoff_max=4, now=clock, sleep=advancing_sleep)
    assert not res.delivered
```

- [ ] **Step 3: Run**

Run: `pytest tests/unit/test_webhook_client.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/webhook/client.py tests/unit/test_webhook_client.py
git commit -m "feat(webhook): raw-body signing + per-attempt fresh timestamp"
```

### Task 22: Executor — Arcadia op + payload builder + status mapping

**Files:**
- Modify: `app/operations/executor.py`, `app/operations/result_cache.py`
- Test: `tests/integration/test_executor.py`, `tests/integration/test_executor_cache.py`

- [ ] **Step 1: Update `result_cache.py` docstring**

Change the `CachedOutcome.status` comment (line 9) to:
`status: str  # "succeeded" | "failed" | "error"` and note only succeeded/failed are cached.

- [ ] **Step 2: Replace `app/operations/executor.py`**

```python
# app/operations/executor.py
import httpx
from pydantic import BaseModel, ValidationError

from app.backends.base import BackendError, GameBackend, TransientBackendError
from app.backends.context import BackendContext
from app.backends.registry import resolve_backend as _resolve_backend
from app.config import Settings
from app.logging import get_logger
from app.operations.dispatch import dispatch
from app.operations.result_cache import CachedOutcome, InMemoryResultCache, ResultCache
from app.postflight.effects import apply_post_effects
from app.preflight.checks import PreflightError, build_context
from app.schemas.requests import Operation
from app.webhook.client import deliver_webhook
from app.webhook.payload import build_webhook_payload

logger = get_logger(__name__)


async def execute_operation(
    payload: dict,
    *,
    session_factory,
    http_client: httpx.AsyncClient,
    settings: Settings,
    result_cache: ResultCache | None = None,
    session_store=None,
    redis=None,
    retry_blocked: bool = False,
    resolve=_resolve_backend,
) -> None:
    if result_cache is None:
        result_cache = InMemoryResultCache()

    # 1. Parse the normalized op (invalid payloads cannot be correlated → log + drop).
    try:
        op = Operation.model_validate(payload)
    except ValidationError as exc:
        logger.error("operation_unparseable_op", error=_summarize(exc))
        return

    key = op.idempotency_key
    log = logger.bind(idempotency_key=key, action=op.action, type=op.type)

    # 0. Retry blocked: a non-idempotent op is being re-run after a crash. Report `error`
    # so Arcadia finalizes in seconds; the backend is NOT called.
    if retry_blocked:
        outcome = CachedOutcome("error", None, "retry_blocked: manual reconcile may be required")
        log.warning("operation_retry_blocked")
        await _deliver(http_client, settings, op, outcome, backend_id=None)
        return

    # 2. Replay short-circuit.
    cached = await result_cache.get(key)
    if cached is not None:
        log.bind(phase="cache_hit").info("operation_replay_from_cache", status=cached.status)
        await _deliver(http_client, settings, op, cached, backend_id=None)
        return

    # 3. Pre-flight (failures reported, not cached).
    try:
        async with session_factory() as session:
            ctx: BackendContext = await build_context(
                session,
                type=op.type,
                backend_name=op.backend_name,
                username=op.username,
                user_id=op.user_id,
                idempotency_key=key,
                account_username=op.account_username,
            )
    except PreflightError as exc:
        await _deliver(http_client, settings, op,
                       CachedOutcome("failed", None, f"preflight_failed: {exc.reason}"),
                       backend_id=None)
        return

    backend_id = ctx.credentials.game_id

    # 4. Resolve backend (config error → failure, not cached).
    try:
        backend: GameBackend = resolve(
            ctx.credentials.backend_driver,
            credentials=ctx.credentials,
            http_client=http_client,
            settings=settings,
            session_store=session_store,
            redis=redis,
        )
    except BackendError as exc:
        await _deliver(http_client, settings, op,
                       CachedOutcome("failed", None, exc.reason), backend_id=backend_id)
        return

    # 5. Backend call.
    log = log.bind(phase="backend_call", backend_id=backend_id)
    try:
        result: BaseModel = await dispatch(backend, op, ctx)
    except TransientBackendError as exc:
        log.warning("operation_backend_transient", reason=exc.reason)
        await _deliver(http_client, settings, op,
                       CachedOutcome("error", None, f"backend_error: {exc.reason}"),
                       backend_id=backend_id)
        return  # not cached → arq re-run retries (capped at 1 for non-idempotent drivers)
    except BackendError as exc:
        outcome = CachedOutcome("failed", None, f"backend_error: {exc.reason}")
        await result_cache.set(key, outcome, settings.result_cache_ttl_seconds)
        log.warning("operation_backend_failed", reason=exc.reason)
        await _deliver(http_client, settings, op, outcome, backend_id=backend_id)
        return
    except ValidationError as exc:
        outcome = CachedOutcome("failed", None, f"invalid_result_payload: {_summarize(exc)}")
        await result_cache.set(key, outcome, settings.result_cache_ttl_seconds)
        log.error("operation_invalid_result", reason=outcome.reason)
        await _deliver(http_client, settings, op, outcome, backend_id=backend_id)
        return
    except Exception:  # noqa: BLE001
        log.exception("operation_unexpected_error")
        await _deliver(http_client, settings, op,
                       CachedOutcome("error", None, "backend_error: unexpected"),
                       backend_id=backend_id)
        return

    outcome = CachedOutcome("succeeded", result.model_dump(exclude_none=True), None)
    await result_cache.set(key, outcome, settings.result_cache_ttl_seconds)
    log.bind(phase="backend_result").info("operation_succeeded")
    await _deliver(http_client, settings, op, outcome, backend_id=backend_id)
    await apply_post_effects(key, op.type, outcome.result or {})


async def _deliver(client, settings: Settings, op: Operation, outcome: CachedOutcome,
                   *, backend_id: int | None) -> None:
    body = build_webhook_payload(op, outcome, backend_id=backend_id)
    await deliver_webhook(
        client,
        settings.webhook_url,
        settings.webhook_secret,
        body,
        max_budget_seconds=settings.webhook_max_budget_seconds,
        backoff_base=settings.webhook_backoff_base,
        backoff_max=settings.webhook_backoff_max,
    )


def _summarize(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "validation error"
    first = errors[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    return f"{loc}: {first.get('msg', 'invalid')}"
```

> Note: on replay (cache hit) `backend_id` is `None` (we don't re-run preflight); Arcadia
> falls back to `backend_name`. If echoing `backend_id` on replays matters, a later iteration
> can store it in the cached outcome — out of scope here.

- [ ] **Step 3: Update `tests/integration/test_executor.py` + `test_executor_cache.py`**

Rework these to drive `execute_operation` with an `Operation`-shaped `payload` dict and the
new `seeded`/mock setup. Representative cases:
- success → webhook body has `action`/`status:"success"` and the right echo fields (assert via
  a respx mock on `settings.webhook_url`).
- `BackendError` → `status:"failed"`, cached; replay re-delivers without re-calling backend.
- `TransientBackendError` → `status:"error"`, not cached.
- `retry_blocked=True` → `status:"error"`, backend never called.
- preflight `game_not_found` (unknown `backend_name`) → `status:"failed"`, message
  `game_not_found: ...`.

Use `payload = Operation(action="recharge", type="RECHARGE", idempotency_key="recharge:t1",
user_id=42, backend_name="milkyway", username="player_one", amount=50,
correlation={"transaction_id":"t1"}).model_dump()` and a fake/mock backend via the `resolve=`
injection point.

- [ ] **Step 4: Run**

Run: `pytest tests/integration/test_executor.py tests/integration/test_executor_cache.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/operations/executor.py app/operations/result_cache.py tests/integration/test_executor.py tests/integration/test_executor_cache.py
git commit -m "feat(executor): Arcadia op + webhook builder + success/failed/error mapping"
```

### Task 23: Worker task — unchanged contract, verify

**Files:**
- Modify (if needed): `app/worker/tasks.py`
- Test: `tests/unit/test_worker_tasks.py`

- [ ] **Step 1: Confirm `app/worker/tasks.py` still matches**

The existing `execute_operation_task` reads `_max_tries`/`job_try` and calls
`execute_operation(payload, ...)`. No change needed — `payload` is now the Operation dict.
Verify the call site still passes `retry_blocked` correctly.

- [ ] **Step 2: Update `tests/unit/test_worker_tasks.py`** to use an Operation-shaped payload
  (e.g. `{"action":"recharge","type":"RECHARGE","idempotency_key":"recharge:t","user_id":1,
  "backend_name":"milkyway","_max_tries":1}`) and assert `retry_blocked` is passed when
  `job_try > _max_tries`.

- [ ] **Step 3: Run**

Run: `pytest tests/unit/test_worker_tasks.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_worker_tasks.py
git commit -m "test(worker): Arcadia op payload"
```

---

## Phase 7 — Logging, env, full-loop, contract, cleanup

### Task 24: Logging redaction set

**Files:**
- Modify: `app/logging.py`
- Test: `tests/unit/test_logging.py`

- [ ] **Step 1: Extend `SECRET_KEYS`**

Add these keys to the `SECRET_KEYS` set (mirror Arcadia's redaction + the new secrets):
`"new_password"`, `"account_created"`, `"user_data"`, `"amount"`, `"api_secret"`,
`"webhook_secret"`, `"x-webhook-signature"`, `"x-request-signature"`.

- [ ] **Step 2: Update `tests/unit/test_logging.py`** — assert `new_password`,
  `account_created`, `amount` are redacted (recursively for `account_created`/`user_data`
  dicts/lists; note `_redact_in_place` recurses dicts — if a list of dicts must be redacted,
  extend `_redact_in_place` to also iterate lists). Add list handling:

```python
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _redact_in_place(item)
```

(Insert into `_redact_in_place` after the dict branch.)

- [ ] **Step 3: Run**

Run: `pytest tests/unit/test_logging.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/logging.py tests/unit/test_logging.py
git commit -m "feat(logging): redact Arcadia secrets + nested account_created"
```

### Task 25: `.env.example` + README pointers

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Replace `.env.example`**

```dotenv
ENV=development
LOG_LEVEL=INFO

# Inbound HMAC — MUST match Arcadia's AUTOMATION_API_SECRET
API_SECRET=change-me-to-match-arcadia-api-secret
# Outbound webhook HMAC — MUST match Arcadia's AUTOMATION_WEBHOOK_SECRET
WEBHOOK_SECRET=change-me-to-match-arcadia-webhook-secret

# Arcadia base URL (webhook -> {APP_URL}/api/automation/webhook)
APP_URL=http://127.0.0.1:8000

# Shared MySQL (read-only user recommended). In Docker, DB_HOST=host.docker.internal
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=arcadia
DB_USER=python_ro
DB_PASSWORD=
DB_DRIVER=asyncmy

REDIS_URL=redis://127.0.0.1:6379/0

WEBHOOK_MAX_BUDGET_SECONDS=600
WEBHOOK_BACKOFF_BASE=0.5
WEBHOOK_BACKOFF_MAX=30

MOCK_FORCE_FAIL=false
MOCK_FORCE_FAIL_REASON=forced mock failure

ANTICAPTCHA_API_KEY=
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "chore(env): Arcadia secrets + DB name"
```

### Task 26: Full-loop integration test

**Files:**
- Modify: `tests/integration/test_full_loop.py`

- [ ] **Step 1: Rewrite the end-to-end test**

Drive a signed `POST /recharge` (using the mock driver — seed a `Game(name="MockGame",
backend_driver="mock")` + matching `GameAccount` in a local fixture or `seeded`), run the
enqueued payload through `execute_operation` with a respx-mocked `webhook_url`, and assert the
webhook body is the Arcadia envelope: `action:"recharge"`, `status:"success"`,
`transaction_id` echoed, `amount` echoed, valid `X-Webhook-Signature` over the raw body.

- [ ] **Step 2: Run**

Run: `pytest tests/integration/test_full_loop.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_full_loop.py
git commit -m "test(full-loop): signed request -> Arcadia webhook envelope"
```

### Task 27: Update the Arcadia contract with Laravel-side specs

**Files:**
- Modify: `/Applications/development/laravel/arcadia/docs/AUTOMATION_SERVICE_CONTRACT.md`

- [ ] **Step 1: Append a "Section 7 — Required Laravel-side changes" block**

Add a new section documenting, with exact column/spec detail, the items the Python service
needs Arcadia to implement:

```markdown
## 7. Required Laravel-side changes (for the automation service)

### 7.1 `games.backend_driver` (REQUIRED)
Add a `string` column `backend_driver` to `games`. The automation service routes each game to
its provider backend by this value. Allowed values:
`mock | gamevault | juwa | juwa2 | gameroom | goldentreasure | milkyway | firekirin |
pandamaster | orionstars | ultrapanda | vblink`. Seed existing rows (e.g. milkyway →
`'milkyway'`).

### 7.2 GameVault-family API credentials (REQUIRED for that family)
Add nullable `string` columns to `games`: `api_base_url`, `api_agent_id`, `api_secret_key`,
`binding_key`. Only the GameVault/Juwa family uses them; session-based providers
(milkyway/gameroom/goldentreasure/ultrapanda) use the existing `backend_url`,`username`,
`password`. Keep `api_secret_key`/`binding_key` out of any API response (`$hidden`).

### 7.3 Persist `id_from_backend` on create (RECOMMENDED)
The create webhook now returns `account_created[0].id_from_backend`. Store it on the new
`GameAccount` so subsequent read/recharge skip a provider-side username search:
`'id_from_backend' => $creds['id_from_backend'] ?? null` in `handleCreate`.

### 7.4 Make `handleCreate` idempotent (RECOMMENDED)
`handleCreate` currently creates a `GameAccount` on every success webhook. Guard against
duplicates (e.g. skip if the user already has an account for the game) so an at-least-once
duplicate webhook can't create two accounts.

### 7.5 Confirmed conventions (no change needed)
- Wire `amount` is **whole dollars** (`(int) ceil($model->...)` after the Eloquent
  `/100` accessor). The automation service treats amounts as dollars end-to-end.
- Webhook `user_data.balance` is **dollars** (decimal). Stored directly into
  `game_reads.current_balance`.
- Webhook `message` on `failed` is user-facing provider text; on `error` it is generic.
```

- [ ] **Step 2: Commit (in the Arcadia repo)**

```bash
cd /Applications/development/laravel/arcadia
git add docs/AUTOMATION_SERVICE_CONTRACT.md
git commit -m "docs(contract): required automation-service columns + recommendations"
cd /Applications/development/python/usgamingclub
```

### Task 28: Sweep remaining tests + full green run

**Files:**
- Modify/verify: `tests/integration/test_*_integration.py` (live-gated, skip when no creds),
  `tests/unit/test_registry.py`, `tests/unit/test_postflight.py`, any stragglers.

- [ ] **Step 1: Run the whole suite**

Run: `make test`
Expected: collect all; fix any remaining references to removed names
(`balance_cents`, `amount_cents`, `total_credit_cents`, `python_signing_secret`,
`build_signature`, `sign`, `verify` (old), `GameOperation`, `operation_adapter`,
`game_id=`/`game_account_id=` preflight kwargs).

- [ ] **Step 2: Fix the live-gated integration tests**

The `test_<driver>_integration.py` files build payloads/assert results. Update any
`amount_cents`/`balance_cents` usage to the dollar-native API; they remain skipped without
live creds, so the change is mechanical (signatures only).

- [ ] **Step 3: Lint + type**

Run: `make lint && make type`
Expected: clean (fix unused imports left by deletions, e.g. `math`).

- [ ] **Step 4: Final full run**

Run: `make test`
Expected: PASS (live-gated integration tests SKIPPED).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test: migrate remaining suites to Arcadia dollar-native boundary"
```

---

## Self-Review Notes (verify during execution)

- **Spec coverage:** every §5.x component maps to a task (5.1 API→T18; 5.2 HMAC→T13;
  5.3 deps→T15; 5.4 schemas→T16/T1; 5.5 dispatch→T19; 5.6 executor→T22; 5.7 payload→T20;
  5.8 client→T21; 5.9 preflight→T12; 5.10 db→T9/T10/T11; 5.11 config→T14; 5.12 usernames→T17;
  5.13 dollar-native backends→T1–T8; logging §8→T24; Laravel asks §9→T27).
- **Type consistency:** result fields are `balance`/`agent_balance` (float), amounts `int`
  dollars, backend methods `recharge(*, amount)`/`redeem(*, amount)`, HMAC funcs
  `verify_request`/`sign_webhook`/`request_signature`/`webhook_signature`, internal model
  `Operation` with `action`/`type`/`correlation`.
- **No placeholders:** every code step contains complete content.
```
