# Python Game Service — Phase 1 (Walking Skeleton) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the full control plane of the Python game service — a signed `POST /operations` that acks `202`, an `arq`+Redis worker that runs a deterministic `MockBackend`, and a signed webhook callback to Laravel — proving the entire request → ack → webhook cycle end-to-end with no real game integration.

**Architecture:** FastAPI API verifies HMAC, parses+dedupes, enqueues an `arq` job (job_id = `idempotency_key`), and returns `202` fast. An `arq` worker runs `execute_operation`: pre-flight reads game creds + account (read-only MySQL), resolves a `GameBackend` (MockBackend in Phase 1), calls the type's method, and delivers the signed webhook with backoff/stop rules. Money/account tables are never written — Laravel applies side effects on the callback.

**Tech Stack:** FastAPI + uvicorn, httpx (async), Pydantic v2 + pydantic-settings, SQLAlchemy 2.0 async + asyncmy (MySQL) / aiosqlite (tests), arq + Redis, structlog, pytest + pytest-asyncio + respx.

**Spec:** `docs/superpowers/specs/2026-06-04-python-game-service-phase1-design.md`
**Wire contract:** `/Applications/development/laravel/casino-app/docs/integrations/python-game-service-api-contract.md`

---

## File structure (created across the tasks below)

```
app/
  __init__.py
  config.py                 # Settings (Task 1)
  logging.py                # structlog + redaction (Task 2)
  security/hmac.py          # sign/verify (Task 3)
  schemas/operations.py     # §4 request union (Task 4)
  schemas/results.py        # §5 result models (Task 5)
  db/models.py              # read-only ORM (Task 6)
  db/engine.py              # async engine/sessionmaker (Task 6)
  db/repositories.py        # repos (Task 7)
  backends/context.py       # BackendContext DTOs (Task 8)
  backends/base.py          # GameBackend protocol + BackendError (Task 8)
  backends/mock/backend.py  # MockBackend (Task 9)
  backends/registry.py      # get_backend (Task 10)
  preflight/checks.py       # build_context (Task 11)
  postflight/effects.py     # no-op seam (Task 12)
  webhook/client.py         # deliver_webhook (Task 13)
  operations/executor.py    # execute_operation (Task 14)
  worker/settings.py        # arq WorkerSettings (Task 15)
  worker/tasks.py           # arq job wrapper (Task 15)
  api/deps.py               # verify_signature dependency (Task 16)
  api/operations.py         # POST /operations (Task 16)
  api/health.py             # /health /ready (Task 17)
  main.py                   # app factory + lifespan (Task 17)
  tools/ping.py             # /webhooks/_ping self-check (Task 18)
tests/                      # unit + integration (throughout)
docker/                     # Dockerfile + compose (Task 19)
docs/                       # README/architecture/runbook/CLAUDE.md (Task 20)
pyproject.toml .env.example .gitignore .dockerignore Makefile  (Task 0)
```

Every `app/<pkg>/` directory gets an empty `__init__.py` (created in Task 0).

---

## Task 0: Repository scaffolding

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.dockerignore`, `.env.example`, `Makefile`
- Create (empty): `app/__init__.py`, `app/security/__init__.py`, `app/schemas/__init__.py`, `app/db/__init__.py`, `app/backends/__init__.py`, `app/backends/mock/__init__.py`, `app/preflight/__init__.py`, `app/postflight/__init__.py`, `app/webhook/__init__.py`, `app/operations/__init__.py`, `app/worker/__init__.py`, `app/api/__init__.py`, `app/tools/__init__.py`, `tests/__init__.py`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "casino-game-service"
version = "0.1.0"
description = "Python game service driving external game backends for the Laravel casino-app."
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.111",
    "uvicorn[standard]>=0.30",
    "httpx>=0.27",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "sqlalchemy[asyncio]>=2.0.30",
    "asyncmy>=0.2.9",
    "arq>=0.26",
    "redis>=5.0",
    "structlog>=24.1",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.2",
    "pytest-asyncio>=0.23",
    "respx>=0.21",
    "aiosqlite>=0.20",
    "ruff>=0.5",
    "mypy>=1.10",
]

[tool.setuptools.packages.find]
include = ["app*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.mypy]
python_version = "3.12"
ignore_missing_imports = true
```

- [ ] **Step 2: Create `.gitignore`**

```gitignore
__pycache__/
*.py[cod]
.venv/
venv/
.env
.env.*
!.env.example
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.egg-info/
.coverage
htmlcov/
.DS_Store
```

- [ ] **Step 3: Create `.dockerignore`**

```dockerignore
.git
.venv
venv
__pycache__
*.pyc
.pytest_cache
.mypy_cache
.ruff_cache
tests
docs
.env
.env.*
!.env.example
```

- [ ] **Step 4: Create `.env.example`**

```dotenv
ENV=development
LOG_LEVEL=INFO

# Shared HMAC secret — MUST match Laravel's PYTHON_SIGNING_SECRET
PYTHON_SIGNING_SECRET=change-me-to-match-laravel

# Laravel base URL (webhook -> {APP_URL}/webhooks/games/operation, ping -> {APP_URL}/webhooks/_ping)
APP_URL=http://127.0.0.1:8000

# Shared MySQL (read-only user recommended). In Docker, DB_HOST=host.docker.internal
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=casino_app
DB_USER=python_ro
DB_PASSWORD=

# Redis (queue + later session/rate-limit). In Docker, redis://redis:6379/0
REDIS_URL=redis://127.0.0.1:6379/0

# Webhook retry budget/backoff
WEBHOOK_MAX_BUDGET_SECONDS=600
WEBHOOK_BACKOFF_BASE=0.5
WEBHOOK_BACKOFF_MAX=30

# Phase-1 MockBackend failure toggle (manual test of the failure path)
MOCK_FORCE_FAIL=false
MOCK_FORCE_FAIL_REASON=forced mock failure

# Phase 3 (unused in Phase 1)
ANTICAPTCHA_API_KEY=
```

- [ ] **Step 5: Create `Makefile`**

```makefile
.PHONY: install test lint type up down ping
install:
	pip install -e ".[dev]"
test:
	pytest -q
lint:
	ruff check app tests
type:
	mypy app
up:
	docker compose -f docker/docker-compose.dev.yml up --build
down:
	docker compose -f docker/docker-compose.dev.yml down
ping:
	python -m app.tools.ping
```

- [ ] **Step 6: Create all empty `__init__.py` files**

```bash
mkdir -p app/security app/schemas app/db app/backends/mock app/preflight app/postflight app/webhook app/operations app/worker app/api app/tools tests
for d in app app/security app/schemas app/db app/backends app/backends/mock app/preflight app/postflight app/webhook app/operations app/worker app/api app/tools tests; do touch "$d/__init__.py"; done
```

- [ ] **Step 7: Install and verify the toolchain**

Run: `pip install -e ".[dev]" && pytest -q`
Expected: install succeeds; pytest reports `no tests ran` (exit 5 is fine — no tests yet).

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml .gitignore .dockerignore .env.example Makefile app tests
git commit -m "chore: scaffold Python game service project (Phase 1)"
```

---

## Task 1: Configuration (`app/config.py`)

**Files:**
- Create: `app/config.py`
- Test: `tests/unit/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_config.py
import importlib
import app.config as config_module


def _fresh_settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    importlib.reload(config_module)
    config_module.get_settings.cache_clear()
    return config_module.get_settings()


def test_settings_read_from_env_and_build_urls(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        PYTHON_SIGNING_SECRET="secret",
        APP_URL="https://laravel.test/",
        DB_NAME="casino",
        DB_USER="ro",
        DB_PASSWORD="pw",
        DB_HOST="db",
        DB_PORT="3307",
    )
    assert s.python_signing_secret == "secret"
    assert s.webhook_url == "https://laravel.test/webhooks/games/operation"
    assert s.ping_url == "https://laravel.test/webhooks/_ping"
    assert s.db_dsn == "mysql+asyncmy://ro:pw@db:3307/casino"
    assert s.replay_window_seconds == 300
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_config.py -v`
Expected: FAIL — `AttributeError`/`ImportError` (no `Settings`/`get_settings`).

- [ ] **Step 3: Write the implementation**

```python
# app/config.py
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    env: str = "development"
    log_level: str = "INFO"

    python_signing_secret: str = ""
    app_url: str = "http://127.0.0.1:8000"

    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_name: str = ""
    db_user: str = ""
    db_password: str = ""

    redis_url: str = "redis://127.0.0.1:6379/0"

    webhook_max_budget_seconds: float = 600.0
    webhook_backoff_base: float = 0.5
    webhook_backoff_max: float = 30.0

    mock_force_fail: bool = False
    mock_force_fail_reason: str = "forced mock failure"

    anticaptcha_api_key: str = ""

    replay_window_seconds: int = 300

    @property
    def webhook_url(self) -> str:
        return f"{self.app_url.rstrip('/')}/webhooks/games/operation"

    @property
    def ping_url(self) -> str:
        return f"{self.app_url.rstrip('/')}/webhooks/_ping"

    @property
    def db_dsn(self) -> str:
        return (
            f"mysql+asyncmy://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/unit/test_config.py
git commit -m "feat(config): env-driven Settings with webhook/ping/db url helpers"
```

---

## Task 2: Structured logging + redaction (`app/logging.py`)

**Files:**
- Create: `app/logging.py`
- Test: `tests/unit/test_logging.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_logging.py
from app.logging import redact_processor, SECRET_KEYS


def test_redact_masks_secret_keys():
    event = {"event": "x", "backend_password": "p", "api_secret_key": "k", "balance_cents": 10}
    out = redact_processor(None, None, event)
    assert out["backend_password"] == "***"
    assert out["api_secret_key"] == "***"
    assert out["balance_cents"] == 10


def test_password_is_a_secret_key():
    assert "password" in SECRET_KEYS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_logging.py -v`
Expected: FAIL — cannot import `redact_processor`.

- [ ] **Step 3: Write the implementation**

```python
# app/logging.py
import logging
import sys

import structlog

from app.config import get_settings

SECRET_KEYS = {
    "password",
    "backend_password",
    "api_secret_key",
    "binding_key",
    "secret",
    "x-signature",
}


def redact_processor(_logger, _name, event_dict):
    for key in list(event_dict.keys()):
        if key.lower() in SECRET_KEYS and event_dict[key] is not None:
            event_dict[key] = "***"
    return event_dict


def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtered_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_logging.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/logging.py tests/unit/test_logging.py
git commit -m "feat(logging): structlog JSON logging with secret redaction"
```

---

## Task 3: HMAC sign/verify (`app/security/hmac.py`)

**Files:**
- Create: `app/security/hmac.py`
- Test: `tests/unit/test_hmac.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_hmac.py
import hashlib
import hmac as _hmac

from app.security.hmac import build_signature, sign, verify

SECRET = "shared-secret"
BODY = '{"idempotency_key":"abc","type":"READ_BALANCE"}'


def test_build_signature_matches_reference_algorithm():
    ts = "1733345678"
    expected = "sha256=" + _hmac.new(
        SECRET.encode(), f"{ts}.{BODY}".encode(), hashlib.sha256
    ).hexdigest()
    assert build_signature(SECRET, ts, BODY) == expected


def test_sign_then_verify_roundtrip():
    headers = sign(SECRET, BODY, timestamp=1000)
    assert headers["Content-Type"] == "application/json"
    assert verify(SECRET, headers["X-Timestamp"], headers["X-Signature"], BODY, now=1000)


def test_verify_rejects_tampered_body():
    headers = sign(SECRET, BODY, timestamp=1000)
    assert not verify(SECRET, headers["X-Timestamp"], headers["X-Signature"], BODY + "x", now=1000)


def test_verify_rejects_expired_timestamp():
    headers = sign(SECRET, BODY, timestamp=1000)
    assert not verify(SECRET, headers["X-Timestamp"], headers["X-Signature"], BODY, now=1000 + 301)


def test_verify_rejects_non_numeric_or_missing():
    assert not verify(SECRET, "not-a-number", "sha256=x", BODY, now=1000)
    assert not verify(SECRET, "1000", "", BODY, now=1000)
    assert not verify("", "1000", "sha256=x", BODY, now=1000)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_hmac.py -v`
Expected: FAIL — module/functions not defined.

- [ ] **Step 3: Write the implementation**

```python
# app/security/hmac.py
import hashlib
import hmac
import time


def build_signature(secret: str, timestamp: str, raw_body: str) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.{raw_body}".encode(), hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def sign(secret: str, raw_body: str, *, timestamp: int | None = None) -> dict[str, str]:
    ts = str(timestamp if timestamp is not None else int(time.time()))
    return {
        "X-Timestamp": ts,
        "X-Signature": build_signature(secret, ts, raw_body),
        "Content-Type": "application/json",
    }


def verify(
    secret: str,
    timestamp: str,
    signature: str,
    raw_body: str,
    *,
    replay_window: int = 300,
    now: int | None = None,
) -> bool:
    if not secret or not timestamp or not signature:
        return False
    if not timestamp.isdigit():
        return False
    current = now if now is not None else int(time.time())
    if abs(current - int(timestamp)) > replay_window:
        return False
    expected = build_signature(secret, timestamp, raw_body)
    return hmac.compare_digest(expected, signature)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_hmac.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/security/hmac.py tests/unit/test_hmac.py
git commit -m "feat(security): HMAC sign/verify matching Laravel scheme (§1)"
```

---

## Task 4: Request schemas (`app/schemas/operations.py`)

**Files:**
- Create: `app/schemas/operations.py`
- Test: `tests/unit/test_schemas_operations.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_schemas_operations.py
import pytest
from pydantic import ValidationError

from app.schemas.operations import operation_adapter


def test_parses_create_account():
    op = operation_adapter.validate_python(
        {"idempotency_key": "k", "type": "CREATE_ACCOUNT", "user_id": 42, "game_id": 7, "game_account_id": None}
    )
    assert op.type == "CREATE_ACCOUNT"
    assert op.game_id == 7


def test_parses_recharge_with_amounts():
    op = operation_adapter.validate_python(
        {"idempotency_key": "k", "type": "RECHARGE", "user_id": 42, "game_id": 7,
         "game_account_id": 1001, "amount_cents": 5000, "bonus_cents": 500, "total_credit_cents": 5500}
    )
    assert op.total_credit_cents == 5500


def test_parses_agent_balance_without_user():
    op = operation_adapter.validate_python(
        {"idempotency_key": "k", "type": "AGENT_BALANCE", "game_id": 7}
    )
    assert op.type == "AGENT_BALANCE"
    assert op.game_id == 7


def test_rejects_unknown_type():
    with pytest.raises(ValidationError):
        operation_adapter.validate_python({"idempotency_key": "k", "type": "NOPE", "game_id": 7})


def test_rejects_recharge_missing_amounts():
    with pytest.raises(ValidationError):
        operation_adapter.validate_python(
            {"idempotency_key": "k", "type": "RECHARGE", "user_id": 42, "game_id": 7, "game_account_id": 1001}
        )


def test_rejects_empty_idempotency_key():
    with pytest.raises(ValidationError):
        operation_adapter.validate_python(
            {"idempotency_key": "", "type": "READ_BALANCE", "user_id": 1, "game_id": 7, "game_account_id": 1}
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_schemas_operations.py -v`
Expected: FAIL — cannot import `operation_adapter`.

- [ ] **Step 3: Write the implementation**

```python
# app/schemas/operations.py
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")
    idempotency_key: str = Field(min_length=1)


class CreateAccountOp(_Base):
    type: Literal["CREATE_ACCOUNT"]
    user_id: int
    game_id: int
    game_account_id: None = None


class ReadBalanceOp(_Base):
    type: Literal["READ_BALANCE"]
    user_id: int
    game_id: int
    game_account_id: int


class ResetPasswordOp(_Base):
    type: Literal["RESET_PASSWORD"]
    user_id: int
    game_id: int
    game_account_id: int


class RechargeOp(_Base):
    type: Literal["RECHARGE"]
    user_id: int
    game_id: int
    game_account_id: int
    amount_cents: int = Field(ge=0)
    bonus_cents: int = Field(ge=0)
    total_credit_cents: int = Field(ge=0)


class RedeemOp(_Base):
    type: Literal["REDEEM"]
    user_id: int
    game_id: int
    game_account_id: int
    amount_cents: int = Field(ge=0)


class AgentBalanceOp(_Base):
    type: Literal["AGENT_BALANCE"]
    game_id: int


OperationRequest = Annotated[
    Union[
        CreateAccountOp,
        ReadBalanceOp,
        ResetPasswordOp,
        RechargeOp,
        RedeemOp,
        AgentBalanceOp,
    ],
    Field(discriminator="type"),
]

operation_adapter: TypeAdapter[OperationRequest] = TypeAdapter(OperationRequest)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_schemas_operations.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add app/schemas/operations.py tests/unit/test_schemas_operations.py
git commit -m "feat(schemas): typed §4 operation request discriminated union"
```

---

## Task 5: Result schemas (`app/schemas/results.py`)

**Files:**
- Create: `app/schemas/results.py`
- Test: `tests/unit/test_schemas_results.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_schemas_results.py
import pytest
from pydantic import ValidationError

from app.schemas.results import (
    AgentBalanceResult,
    CreateAccountResult,
    ReadBalanceResult,
    RechargeResult,
    ResetPasswordResult,
)


def test_create_account_dump_omits_none_external_id():
    r = CreateAccountResult(username="u", password="p")
    assert r.model_dump(exclude_none=True) == {"username": "u", "password": "p"}


def test_create_account_rejects_empty_external_id():
    with pytest.raises(ValidationError):
        CreateAccountResult(username="u", password="p", external_user_id="")


def test_read_balance_requires_non_negative_int():
    assert ReadBalanceResult(balance_cents=0).balance_cents == 0
    with pytest.raises(ValidationError):
        ReadBalanceResult(balance_cents=-1)


def test_recharge_balance_optional_and_omitted_when_none():
    assert RechargeResult().model_dump(exclude_none=True) == {}


def test_agent_balance_required():
    assert AgentBalanceResult(agent_balance_cents=100).agent_balance_cents == 100
    with pytest.raises(ValidationError):
        AgentBalanceResult()


def test_reset_password_required():
    assert ResetPasswordResult(password="x").password == "x"
    with pytest.raises(ValidationError):
        ResetPasswordResult(password="")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_schemas_results.py -v`
Expected: FAIL — cannot import result models.

- [ ] **Step 3: Write the implementation**

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
    balance_cents: int = Field(ge=0)


class ResetPasswordResult(_Result):
    password: str = Field(min_length=1)


class RechargeResult(_Result):
    balance_cents: int | None = Field(default=None, ge=0)


class RedeemResult(_Result):
    balance_cents: int | None = Field(default=None, ge=0)


class AgentBalanceResult(_Result):
    agent_balance_cents: int = Field(ge=0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_schemas_results.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add app/schemas/results.py tests/unit/test_schemas_results.py
git commit -m "feat(schemas): typed §5 result models with contract validation"
```

---

## Task 6: DB models + engine + test fixtures (`app/db/models.py`, `app/db/engine.py`, `tests/conftest.py`)

**Files:**
- Create: `app/db/models.py`, `app/db/engine.py`, `tests/conftest.py`
- Test: `tests/unit/test_db_models.py`

- [ ] **Step 1: Write the shared async test fixtures (`tests/conftest.py`)**

```python
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
```

- [ ] **Step 2: Write the failing test**

```python
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/unit/test_db_models.py -v`
Expected: FAIL — cannot import models.

- [ ] **Step 4: Write the models (`app/db/models.py`)**

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
    active: Mapped[bool] = mapped_column(default=False)
    backend_url: Mapped[str | None] = mapped_column(default=None)
    login_page_url: Mapped[str | None] = mapped_column(default=None)
    game_url: Mapped[str | None] = mapped_column(default=None)
    backend_username: Mapped[str | None] = mapped_column(default=None)
    backend_password: Mapped[str | None] = mapped_column(default=None)
    api_base_url: Mapped[str | None] = mapped_column(default=None)
    api_agent_id: Mapped[str | None] = mapped_column(default=None)
    api_secret_key: Mapped[str | None] = mapped_column(default=None)
    binding_key: Mapped[str | None] = mapped_column(default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(default=None)


class GameAccount(Base):
    __tablename__ = "game_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int]
    game_id: Mapped[int]
    username: Mapped[str]
    password: Mapped[str]
    external_user_id: Mapped[str | None] = mapped_column(default=None)
    balance_cents: Mapped[int | None] = mapped_column(default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(default=None)


class GameOperation(Base):
    __tablename__ = "game_operations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int]
    game_id: Mapped[int]
    game_account_id: Mapped[int | None] = mapped_column(default=None)
    type: Mapped[str]
    status: Mapped[str]
    idempotency_key: Mapped[str]
```

- [ ] **Step 5: Write the engine (`app/db/engine.py`)**

```python
# app/db/engine.py
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().db_dsn, pool_pre_ping=True, pool_recycle=3600)
    return _engine


def get_sessionmaker() -> async_sessionmaker:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/unit/test_db_models.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/db/models.py app/db/engine.py tests/conftest.py tests/unit/test_db_models.py
git commit -m "feat(db): read-only ORM models, async engine, sqlite test fixtures"
```

---

## Task 7: Repositories (`app/db/repositories.py`)

**Files:**
- Create: `app/db/repositories.py`
- Test: `tests/unit/test_repositories.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_repositories.py -v`
Expected: FAIL — cannot import repositories.

- [ ] **Step 3: Write the implementation**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_repositories.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/db/repositories.py tests/unit/test_repositories.py
git commit -m "feat(db): read-only repositories for games/accounts/operations"
```

---

## Task 8: Backend context + protocol (`app/backends/context.py`, `app/backends/base.py`)

**Files:**
- Create: `app/backends/context.py`, `app/backends/base.py`
- Test: `tests/unit/test_backend_base.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_backend_base.py
from app.backends.base import BackendError
from app.backends.context import AccountIdentity, BackendContext, GameCredentials


def test_backend_error_carries_reason():
    err = BackendError("game backend timeout")
    assert err.reason == "game backend timeout"
    assert str(err) == "game backend timeout"


def test_context_dataclasses_construct():
    creds = GameCredentials(
        game_id=7, name="Demo", backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url="x", api_agent_id="a", api_secret_key="s", binding_key="b",
    )
    acct = AccountIdentity(game_account_id=1001, user_id=42, game_id=7, username="u", external_user_id="E")
    ctx = BackendContext(credentials=creds, user_id=42, account=acct)
    assert ctx.credentials.game_id == 7
    assert ctx.account.username == "u"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_backend_base.py -v`
Expected: FAIL — modules not defined.

- [ ] **Step 3: Write `app/backends/context.py`**

```python
# app/backends/context.py
from dataclasses import dataclass


@dataclass(frozen=True)
class GameCredentials:
    game_id: int
    name: str
    backend_url: str | None
    login_page_url: str | None
    backend_username: str | None
    backend_password: str | None
    api_base_url: str | None
    api_agent_id: str | None
    api_secret_key: str | None
    binding_key: str | None


@dataclass(frozen=True)
class AccountIdentity:
    game_account_id: int
    user_id: int
    game_id: int
    username: str
    external_user_id: str | None


@dataclass(frozen=True)
class BackendContext:
    credentials: GameCredentials
    user_id: int | None
    account: AccountIdentity | None
```

- [ ] **Step 4: Write `app/backends/base.py`**

```python
# app/backends/base.py
from typing import Protocol

from app.backends.context import BackendContext
from app.schemas.results import (
    AgentBalanceResult,
    CreateAccountResult,
    ReadBalanceResult,
    RechargeResult,
    RedeemResult,
    ResetPasswordResult,
)


class BackendError(Exception):
    """Raised when a game backend call fails in a way that should be reported as status:failed."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class GameBackend(Protocol):
    async def create_account(self, ctx: BackendContext) -> CreateAccountResult: ...

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult: ...

    async def reset_password(self, ctx: BackendContext) -> ResetPasswordResult: ...

    async def recharge(
        self, ctx: BackendContext, *, amount_cents: int, bonus_cents: int, total_credit_cents: int
    ) -> RechargeResult: ...

    async def redeem(self, ctx: BackendContext, *, amount_cents: int) -> RedeemResult: ...

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult: ...
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_backend_base.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/backends/context.py app/backends/base.py tests/unit/test_backend_base.py
git commit -m "feat(backends): BackendContext DTOs + GameBackend protocol + BackendError"
```

---

## Task 9: MockBackend (`app/backends/mock/backend.py`)

**Files:**
- Create: `app/backends/mock/backend.py`
- Test: `tests/unit/test_mock_backend.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_mock_backend.py
import pytest

from app.backends.base import BackendError
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.mock.backend import MockBackend


def _creds(game_id=7):
    return GameCredentials(
        game_id=game_id, name="Demo", backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url=None, api_agent_id=None, api_secret_key=None, binding_key=None,
    )


def _ctx(account=True):
    acct = AccountIdentity(game_account_id=1001, user_id=42, game_id=7, username="plyr_42", external_user_id="EXT-42") if account else None
    return BackendContext(credentials=_creds(), user_id=42, account=acct)


async def test_create_account_is_deterministic():
    b = MockBackend()
    r1 = await b.create_account(_ctx(account=False))
    r2 = await b.create_account(_ctx(account=False))
    assert r1.username == r2.username == "mock_42_7"
    assert r1.password and r1.external_user_id


async def test_recharge_echoes_total_credit_as_balance():
    r = await MockBackend().recharge(_ctx(), amount_cents=5000, bonus_cents=500, total_credit_cents=5500)
    assert r.balance_cents == 5500


async def test_agent_balance_returns_value():
    r = await MockBackend().agent_balance(_ctx(account=False))
    assert r.agent_balance_cents >= 0


async def test_fail_mode_raises_backend_error():
    b = MockBackend(fail=True, fail_reason="boom")
    with pytest.raises(BackendError) as ei:
        await b.read_balance(_ctx())
    assert ei.value.reason == "boom"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_mock_backend.py -v`
Expected: FAIL — cannot import `MockBackend`.

- [ ] **Step 3: Write the implementation**

```python
# app/backends/mock/backend.py
from app.backends.base import BackendError
from app.backends.context import BackendContext
from app.schemas.results import (
    AgentBalanceResult,
    CreateAccountResult,
    ReadBalanceResult,
    RechargeResult,
    RedeemResult,
    ResetPasswordResult,
)


class MockBackend:
    """Deterministic, contract-valid backend used to prove the control plane (Phase 1)."""

    def __init__(self, *, fail: bool = False, fail_reason: str = "forced mock failure") -> None:
        self._fail = fail
        self._fail_reason = fail_reason

    def _maybe_fail(self) -> None:
        if self._fail:
            raise BackendError(self._fail_reason)

    async def create_account(self, ctx: BackendContext) -> CreateAccountResult:
        self._maybe_fail()
        uid = ctx.user_id
        gid = ctx.credentials.game_id
        return CreateAccountResult(
            username=f"mock_{uid}_{gid}",
            password="MockPass123!",
            external_user_id=f"EXT{uid}{gid}",
        )

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        self._maybe_fail()
        return ReadBalanceResult(balance_cents=12750)

    async def reset_password(self, ctx: BackendContext) -> ResetPasswordResult:
        self._maybe_fail()
        return ResetPasswordResult(password="MockReset123!")

    async def recharge(
        self, ctx: BackendContext, *, amount_cents: int, bonus_cents: int, total_credit_cents: int
    ) -> RechargeResult:
        self._maybe_fail()
        return RechargeResult(balance_cents=total_credit_cents)

    async def redeem(self, ctx: BackendContext, *, amount_cents: int) -> RedeemResult:
        self._maybe_fail()
        return RedeemResult(balance_cents=0)

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult:
        self._maybe_fail()
        return AgentBalanceResult(agent_balance_cents=100_000)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_mock_backend.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/mock/backend.py tests/unit/test_mock_backend.py
git commit -m "feat(backends): deterministic MockBackend with failure mode"
```

---

## Task 10: Backend registry (`app/backends/registry.py`)

**Files:**
- Create: `app/backends/registry.py`
- Test: `tests/unit/test_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_registry.py
from app.backends.mock.backend import MockBackend
from app.backends.registry import get_backend
from app.config import get_settings


def test_registry_returns_mock_backend_phase1():
    get_settings.cache_clear()
    backend = get_backend(7)
    assert isinstance(backend, MockBackend)


def test_registry_honors_force_fail(monkeypatch):
    monkeypatch.setenv("MOCK_FORCE_FAIL", "true")
    monkeypatch.setenv("MOCK_FORCE_FAIL_REASON", "manual")
    get_settings.cache_clear()
    backend = get_backend(7)
    assert backend._fail is True and backend._fail_reason == "manual"
    get_settings.cache_clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_registry.py -v`
Expected: FAIL — cannot import `get_backend`.

- [ ] **Step 3: Write the implementation**

```python
# app/backends/registry.py
from app.backends.base import GameBackend
from app.backends.mock.backend import MockBackend
from app.config import get_settings


def get_backend(game_id: int) -> GameBackend:
    """Resolve the backend for a game. Phase 1: every game uses the MockBackend.

    Later phases map specific game_ids to real backend modules here.
    """
    settings = get_settings()
    return MockBackend(fail=settings.mock_force_fail, fail_reason=settings.mock_force_fail_reason)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_registry.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/backends/registry.py tests/unit/test_registry.py
git commit -m "feat(backends): registry resolving game_id -> backend (mock in Phase 1)"
```

---

## Task 11: Pre-flight checks (`app/preflight/checks.py`)

**Files:**
- Create: `app/preflight/checks.py`
- Test: `tests/unit/test_preflight.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_preflight.py
import pytest

from app.preflight.checks import PreflightError, build_context


async def test_account_scoped_context_loads_account(seeded):
    async with seeded() as s:
        ctx = await build_context(
            s, type="READ_BALANCE", user_id=42, game_id=7, game_account_id=1001
        )
    assert ctx.credentials.api_agent_id == "agent-1"
    assert ctx.account is not None and ctx.account.username == "plyr_42"


async def test_create_account_has_no_account(seeded):
    async with seeded() as s:
        ctx = await build_context(
            s, type="CREATE_ACCOUNT", user_id=42, game_id=7, game_account_id=None
        )
    assert ctx.account is None
    assert ctx.user_id == 42


async def test_missing_game_raises(seeded):
    async with seeded() as s:
        with pytest.raises(PreflightError) as ei:
            await build_context(s, type="AGENT_BALANCE", user_id=None, game_id=999, game_account_id=None)
    assert "game_not_found" in ei.value.reason


async def test_missing_account_raises(seeded):
    async with seeded() as s:
        with pytest.raises(PreflightError) as ei:
            await build_context(s, type="REDEEM", user_id=42, game_id=7, game_account_id=999)
    assert "game_account_not_found" in ei.value.reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_preflight.py -v`
Expected: FAIL — cannot import `build_context`.

- [ ] **Step 3: Write the implementation**

```python
# app/preflight/checks.py
from sqlalchemy.ext.asyncio import AsyncSession

from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.db.repositories import GameAccountsRepository, GamesRepository

ACCOUNT_SCOPED_TYPES = {"READ_BALANCE", "RESET_PASSWORD", "RECHARGE", "REDEEM"}


class PreflightError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


async def build_context(
    session: AsyncSession,
    *,
    type: str,
    user_id: int | None,
    game_id: int,
    game_account_id: int | None,
) -> BackendContext:
    game = await GamesRepository(session).get(game_id)
    if game is None:
        raise PreflightError(f"game_not_found: {game_id}")

    credentials = GameCredentials(
        game_id=game.id,
        name=game.name,
        backend_url=game.backend_url,
        login_page_url=game.login_page_url,
        backend_username=game.backend_username,
        backend_password=game.backend_password,
        api_base_url=game.api_base_url,
        api_agent_id=game.api_agent_id,
        api_secret_key=game.api_secret_key,
        binding_key=game.binding_key,
    )

    account: AccountIdentity | None = None
    if type in ACCOUNT_SCOPED_TYPES:
        if game_account_id is None:
            raise PreflightError("missing_game_account_id")
        acct = await GameAccountsRepository(session).get(game_account_id)
        if acct is None:
            raise PreflightError(f"game_account_not_found: {game_account_id}")
        account = AccountIdentity(
            game_account_id=acct.id,
            user_id=acct.user_id,
            game_id=acct.game_id,
            username=acct.username,
            external_user_id=acct.external_user_id,
        )

    return BackendContext(credentials=credentials, user_id=user_id, account=account)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_preflight.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/preflight/checks.py tests/unit/test_preflight.py
git commit -m "feat(preflight): build BackendContext from games/game_accounts reads"
```

---

## Task 12: Post-flight no-op seam (`app/postflight/effects.py`)

**Files:**
- Create: `app/postflight/effects.py`
- Test: `tests/unit/test_postflight.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_postflight.py
from app.postflight.effects import apply_post_effects


async def test_apply_post_effects_is_noop_and_returns_none():
    result = await apply_post_effects("idem-key", "READ_BALANCE", {"balance_cents": 1})
    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_postflight.py -v`
Expected: FAIL — cannot import `apply_post_effects`.

- [ ] **Step 3: Write the implementation**

```python
# app/postflight/effects.py
"""Post-flight side-effect seam.

Per the contract, Laravel applies all money/account side effects when it processes the
webhook callback. Python writes nothing. This hook exists so future phases have a place
to record read-only telemetry; in Phase 1 it is intentionally a no-op.
"""


async def apply_post_effects(idempotency_key: str, op_type: str, result_payload: dict) -> None:
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_postflight.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/postflight/effects.py tests/unit/test_postflight.py
git commit -m "feat(postflight): no-op side-effect seam (Laravel owns side effects)"
```

---

## Task 13: Webhook client (`app/webhook/client.py`)

**Files:**
- Create: `app/webhook/client.py`
- Test: `tests/unit/test_webhook_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_webhook_client.py
import httpx
import respx

from app.security.hmac import verify
from app.webhook.client import deliver_webhook

URL = "https://laravel.test/webhooks/games/operation"
SECRET = "s"
PAYLOAD = {"idempotency_key": "k", "status": "succeeded", "result": {"balance_cents": 1}}


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


async def _noop_sleep(_seconds):
    return None


@respx.mock
async def test_delivers_on_200_and_signs_request():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    async with httpx.AsyncClient() as client:
        res = await deliver_webhook(client, URL, SECRET, PAYLOAD, max_budget_seconds=600)
    assert res.delivered is True and res.status_code == 200 and res.attempts == 1
    sent = route.calls.last.request
    assert verify(
        SECRET,
        sent.headers["X-Timestamp"],
        sent.headers["X-Signature"],
        sent.content.decode(),
    )


@respx.mock
async def test_retries_on_500_then_succeeds():
    respx.post(URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json={"ok": True})]
    )
    async with httpx.AsyncClient() as client:
        res = await deliver_webhook(
            client, URL, SECRET, PAYLOAD, max_budget_seconds=600, sleep=_noop_sleep
        )
    assert res.delivered is True and res.attempts == 2


@respx.mock
async def test_does_not_retry_on_401():
    route = respx.post(URL).mock(return_value=httpx.Response(401))
    async with httpx.AsyncClient() as client:
        res = await deliver_webhook(
            client, URL, SECRET, PAYLOAD, max_budget_seconds=600, sleep=_noop_sleep
        )
    assert res.delivered is False and res.status_code == 401 and route.call_count == 1


@respx.mock
async def test_gives_up_after_budget():
    respx.post(URL).mock(return_value=httpx.Response(500))
    clock = FakeClock()

    async def advancing_sleep(seconds):
        clock.t += seconds

    async with httpx.AsyncClient() as client:
        res = await deliver_webhook(
            client, URL, SECRET, PAYLOAD,
            max_budget_seconds=5, backoff_base=1, backoff_max=4,
            now=clock, sleep=advancing_sleep,
        )
    assert res.delivered is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_webhook_client.py -v`
Expected: FAIL — cannot import `deliver_webhook`.

- [ ] **Step 3: Write the implementation**

```python
# app/webhook/client.py
import asyncio
import json
import random
import time
from dataclasses import dataclass

import httpx

from app.logging import get_logger
from app.security.hmac import sign

logger = get_logger(__name__)

NO_RETRY_STATUSES = {401, 422}


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
    sleep=asyncio.sleep,
) -> WebhookResult:
    raw = json.dumps(payload, separators=(",", ":"))
    deadline = now() + max_budget_seconds
    attempt = 0
    last_status: int | None = None

    while True:
        attempt += 1
        # Re-sign every attempt: the 300s replay window means a stale timestamp
        # would be rejected once retries span more than five minutes.
        headers = sign(secret, raw)
        try:
            resp = await client.post(url, content=raw.encode(), headers=headers)
            last_status = resp.status_code
            if last_status == 200:
                logger.info("webhook_delivered", phase="webhook_delivered", attempts=attempt)
                return WebhookResult(True, 200, attempt)
            if last_status in NO_RETRY_STATUSES:
                logger.error(
                    "webhook_sender_bug", phase="webhook_attempt", status=last_status
                )
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

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_webhook_client.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/webhook/client.py tests/unit/test_webhook_client.py
git commit -m "feat(webhook): signed delivery with backoff and §3 stop rules"
```

---

## Task 14: Operation executor (`app/operations/executor.py`)

**Files:**
- Create: `app/operations/executor.py`
- Test: `tests/integration/test_executor.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_executor.py
import httpx
import respx

from app.config import Settings
from app.operations.executor import execute_operation

URL = "https://laravel.test/webhooks/games/operation"


def _settings():
    return Settings(
        python_signing_secret="s",
        app_url="https://laravel.test",
        webhook_max_budget_seconds=600,
    )


async def _run(payload, session_factory):
    async with httpx.AsyncClient() as client:
        await execute_operation(
            payload,
            session_factory=session_factory,
            http_client=client,
            settings=_settings(),
        )


@respx.mock
async def test_read_balance_success_posts_succeeded_webhook(seeded):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await _run(
        {"idempotency_key": "k1", "type": "READ_BALANCE", "user_id": 42, "game_id": 7, "game_account_id": 1001},
        seeded,
    )
    body = route.calls.last.request.content.decode()
    assert '"status":"succeeded"' in body
    assert '"balance_cents":12750' in body
    assert '"idempotency_key":"k1"' in body


@respx.mock
async def test_create_account_includes_username_password(seeded):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await _run(
        {"idempotency_key": "k2", "type": "CREATE_ACCOUNT", "user_id": 42, "game_id": 7, "game_account_id": None},
        seeded,
    )
    body = route.calls.last.request.content.decode()
    assert '"username":"mock_42_7"' in body and '"password":"' in body


@respx.mock
async def test_preflight_failure_posts_failed_webhook(seeded):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await _run(
        {"idempotency_key": "k3", "type": "REDEEM", "user_id": 42, "game_id": 7, "game_account_id": 999, "amount_cents": 100},
        seeded,
    )
    body = route.calls.last.request.content.decode()
    assert '"status":"failed"' in body and "game_account_not_found" in body


@respx.mock
async def test_invalid_payload_posts_failed_webhook(seeded):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await _run({"idempotency_key": "k4", "type": "NOPE"}, seeded)
    body = route.calls.last.request.content.decode()
    assert '"status":"failed"' in body and "invalid_payload" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_executor.py -v`
Expected: FAIL — cannot import `execute_operation`.

- [ ] **Step 3: Write the implementation**

```python
# app/operations/executor.py
import httpx
from pydantic import BaseModel, ValidationError

from app.backends.base import BackendError, GameBackend
from app.backends.context import BackendContext
from app.backends.registry import get_backend
from app.config import Settings
from app.logging import get_logger
from app.operations.dispatch import dispatch
from app.postflight.effects import apply_post_effects
from app.preflight.checks import PreflightError, build_context
from app.schemas.operations import operation_adapter
from app.webhook.client import deliver_webhook

logger = get_logger(__name__)


async def execute_operation(
    payload: dict,
    *,
    session_factory,
    http_client: httpx.AsyncClient,
    settings: Settings,
    backend_resolver=get_backend,
) -> None:
    key = str(payload.get("idempotency_key", ""))
    log = logger.bind(idempotency_key=key, phase="received")

    try:
        op = operation_adapter.validate_python(payload)
    except ValidationError as exc:
        reason = f"invalid_payload: {_summarize(exc)}"
        log.warning("operation_invalid", reason=reason)
        await _report_failure(http_client, settings, key, reason)
        return

    log = log.bind(type=op.type, game_id=op.game_id, phase="preflight")
    try:
        async with session_factory() as session:
            ctx: BackendContext = await build_context(
                session,
                type=op.type,
                user_id=getattr(op, "user_id", None),
                game_id=op.game_id,
                game_account_id=getattr(op, "game_account_id", None),
            )
    except PreflightError as exc:
        reason = f"preflight_failed: {exc.reason}"
        log.warning("operation_preflight_failed", reason=reason)
        await _report_failure(http_client, settings, key, reason)
        return

    backend: GameBackend = backend_resolver(op.game_id)
    log = log.bind(phase="backend_call")
    try:
        result: BaseModel = await dispatch(backend, op, ctx)
    except BackendError as exc:
        reason = f"backend_error: {exc.reason}"
        log.warning("operation_backend_failed", reason=reason)
        await _report_failure(http_client, settings, key, reason)
        return
    except ValidationError as exc:
        reason = f"invalid_result_payload: {_summarize(exc)}"
        log.error("operation_invalid_result", reason=reason)
        await _report_failure(http_client, settings, key, reason)
        return
    except Exception:  # noqa: BLE001 - any unexpected error becomes a reported failure
        log.exception("operation_unexpected_error")
        await _report_failure(http_client, settings, key, "backend_error: unexpected")
        return

    result_payload = result.model_dump(exclude_none=True)
    log.bind(phase="backend_result").info(
        "operation_succeeded", result_keys=sorted(result_payload.keys())
    )
    await _report_success(http_client, settings, key, result_payload)
    await apply_post_effects(key, op.type, result_payload)


async def _report_success(client, settings: Settings, key: str, result_payload: dict) -> None:
    await deliver_webhook(
        client,
        settings.webhook_url,
        settings.python_signing_secret,
        {"idempotency_key": key, "status": "succeeded", "result": result_payload},
        max_budget_seconds=settings.webhook_max_budget_seconds,
        backoff_base=settings.webhook_backoff_base,
        backoff_max=settings.webhook_backoff_max,
    )


async def _report_failure(client, settings: Settings, key: str, reason: str) -> None:
    await deliver_webhook(
        client,
        settings.webhook_url,
        settings.python_signing_secret,
        {"idempotency_key": key, "status": "failed", "reason": reason[:255]},
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

- [ ] **Step 4: Write the dispatch helper (`app/operations/dispatch.py`)**

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
    if op.type == "RECHARGE":
        return await backend.recharge(
            ctx,
            amount_cents=op.amount_cents,
            bonus_cents=op.bonus_cents,
            total_credit_cents=op.total_credit_cents,
        )
    if op.type == "REDEEM":
        return await backend.redeem(ctx, amount_cents=op.amount_cents)
    if op.type == "AGENT_BALANCE":
        return await backend.agent_balance(ctx)
    raise BackendError(f"unsupported_type: {op.type}")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/integration/test_executor.py -v`
Expected: PASS (4 tests). (Create `tests/integration/__init__.py` if needed: `touch tests/integration/__init__.py`.)

- [ ] **Step 6: Commit**

```bash
git add app/operations/executor.py app/operations/dispatch.py tests/integration/test_executor.py tests/integration/__init__.py
git commit -m "feat(operations): executor orchestrating preflight->backend->webhook"
```

---

## Task 15: arq worker (`app/worker/settings.py`, `app/worker/tasks.py`)

**Files:**
- Create: `app/worker/tasks.py`, `app/worker/settings.py`
- Test: `tests/unit/test_worker_tasks.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_worker_tasks.py
import app.worker.tasks as tasks


async def test_task_delegates_to_executor(monkeypatch, seeded):
    captured = {}

    async def fake_execute(payload, **kwargs):
        captured["payload"] = payload
        captured["kwargs"] = kwargs

    monkeypatch.setattr(tasks, "execute_operation", fake_execute)

    class FakeClient:
        pass

    ctx = {"http_client": FakeClient(), "session_factory": seeded}
    payload = {"idempotency_key": "k", "type": "READ_BALANCE", "user_id": 42, "game_id": 7, "game_account_id": 1001}
    await tasks.execute_operation_task(ctx, payload)

    assert captured["payload"] == payload
    assert captured["kwargs"]["http_client"] is ctx["http_client"]
    assert captured["kwargs"]["session_factory"] is seeded
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_worker_tasks.py -v`
Expected: FAIL — cannot import `app.worker.tasks`.

- [ ] **Step 3: Write `app/worker/tasks.py`**

```python
# app/worker/tasks.py
from app.config import get_settings
from app.operations.executor import execute_operation


async def execute_operation_task(ctx: dict, payload: dict) -> None:
    await execute_operation(
        payload,
        session_factory=ctx["session_factory"],
        http_client=ctx["http_client"],
        settings=get_settings(),
    )
```

- [ ] **Step 4: Write `app/worker/settings.py`**

```python
# app/worker/settings.py
import httpx
from arq.connections import RedisSettings

from app.config import get_settings
from app.db.engine import get_sessionmaker
from app.logging import configure_logging
from app.worker.tasks import execute_operation_task


async def startup(ctx: dict) -> None:
    configure_logging()
    ctx["http_client"] = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    ctx["session_factory"] = get_sessionmaker()


async def shutdown(ctx: dict) -> None:
    await ctx["http_client"].aclose()


class WorkerSettings:
    functions = [execute_operation_task]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # Job timeout must exceed the webhook retry budget so a still-retrying job is not killed.
    job_timeout = int(get_settings().webhook_max_budget_seconds) + 60
    max_tries = 3  # backstop for worker crashes; executor is safe to re-run in Phase 1
    keep_result = 0
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_worker_tasks.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/worker/tasks.py app/worker/settings.py tests/unit/test_worker_tasks.py
git commit -m "feat(worker): arq worker settings + task delegating to executor"
```

---

## Task 16: API — signature dependency + `/operations` (`app/api/deps.py`, `app/api/operations.py`)

**Files:**
- Create: `app/api/deps.py`, `app/api/operations.py`
- Test: `tests/integration/test_operations_endpoint.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_operations_endpoint.py
import json

import httpx
import pytest

from app.api.deps import verify_signature
from app.api.operations import router
from app.config import get_settings
from app.security.hmac import sign
from fastapi import FastAPI


class FakeArq:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, func, payload, _job_id=None):
        self.jobs.append((func, payload, _job_id))
        return object()


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("PYTHON_SIGNING_SECRET", "s")
    monkeypatch.setenv("APP_URL", "https://laravel.test")
    get_settings.cache_clear()
    application = FastAPI()
    application.include_router(router)
    application.state.arq = FakeArq()
    return application


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_valid_trigger_acks_202_and_enqueues(app):
    body = json.dumps(
        {"idempotency_key": "k1", "type": "READ_BALANCE", "user_id": 42, "game_id": 7, "game_account_id": 1001},
        separators=(",", ":"),
    )
    headers = sign("s", body)
    async with await _client(app) as c:
        resp = await c.post("/operations", content=body, headers=headers)
    assert resp.status_code == 202
    assert app.state.arq.jobs[0][2] == "k1"  # _job_id == idempotency_key


async def test_bad_signature_returns_401(app):
    body = json.dumps({"idempotency_key": "k1", "type": "READ_BALANCE"}, separators=(",", ":"))
    async with await _client(app) as c:
        resp = await c.post("/operations", content=body, headers={"X-Timestamp": "1", "X-Signature": "sha256=bad"})
    assert resp.status_code == 401
    assert app.state.arq.jobs == []


async def test_missing_idempotency_key_returns_400(app):
    body = json.dumps({"type": "READ_BALANCE"}, separators=(",", ":"))
    headers = sign("s", body)
    async with await _client(app) as c:
        resp = await c.post("/operations", content=body, headers=headers)
    assert resp.status_code == 400
    assert app.state.arq.jobs == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_operations_endpoint.py -v`
Expected: FAIL — cannot import `verify_signature`/`router`.

- [ ] **Step 3: Write `app/api/deps.py`**

```python
# app/api/deps.py
from fastapi import HTTPException, Request

from app.config import get_settings
from app.security.hmac import verify


async def verify_signature(request: Request) -> bytes:
    raw = await request.body()
    settings = get_settings()
    ok = verify(
        settings.python_signing_secret,
        request.headers.get("X-Timestamp", ""),
        request.headers.get("X-Signature", ""),
        raw.decode("utf-8", errors="replace"),
        replay_window=settings.replay_window_seconds,
    )
    if not ok:
        raise HTTPException(status_code=401, detail="Signature invalid")
    return raw
```

- [ ] **Step 4: Write `app/api/operations.py`**

```python
# app/api/operations.py
import json

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import verify_signature
from app.logging import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.post("/operations")
async def receive_operation(
    request: Request, raw: bytes = Depends(verify_signature)
) -> Response:
    # Signature verified by the dependency. Parse only enough to correlate + dedupe.
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("operation_unparseable_body", phase="received")
        return Response(status_code=400)  # cannot correlate -> Laravel marks dispatch_failed

    key = data.get("idempotency_key") if isinstance(data, dict) else None
    if not isinstance(key, str) or key == "":
        logger.warning("operation_missing_idempotency_key", phase="received")
        return Response(status_code=400)

    await request.app.state.arq.enqueue_job("execute_operation_task", data, _job_id=key)
    logger.bind(idempotency_key=key, phase="enqueued").info("operation_enqueued")
    return Response(status_code=202)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/integration/test_operations_endpoint.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add app/api/deps.py app/api/operations.py tests/integration/test_operations_endpoint.py
git commit -m "feat(api): POST /operations (verify->parse->dedupe->enqueue->202)"
```

---

## Task 17: API — health endpoints + app factory (`app/api/health.py`, `app/main.py`)

**Files:**
- Create: `app/api/health.py`, `app/main.py`
- Test: `tests/integration/test_health.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_health.py
import httpx

from app.api.health import router
from fastapi import FastAPI


async def test_health_returns_ok():
    app = FastAPI()
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/health")
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_health.py -v`
Expected: FAIL — cannot import `app.api.health`.

- [ ] **Step 3: Write `app/api/health.py`**

```python
# app/api/health.py
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db.engine import get_engine

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    checks = {"db": False, "redis": False}
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["db"] = True
    except Exception:  # noqa: BLE001
        pass
    try:
        arq = getattr(request.app.state, "arq", None)
        if arq is not None:
            await arq.ping()
            checks["redis"] = True
    except Exception:  # noqa: BLE001
        pass
    ok = all(checks.values())
    return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)
```

- [ ] **Step 4: Write `app/main.py`**

```python
# app/main.py
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI

from app.api import health, operations
from app.config import get_settings
from app.logging import configure_logging, get_logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    get_logger(__name__).info("service_started", env=settings.env)
    try:
        yield
    finally:
        await app.state.arq.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Casino Game Service", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(operations.router)
    return app


app = create_app()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/integration/test_health.py -v`
Expected: PASS.

- [ ] **Step 6: Verify the full app imports**

Run: `python -c "from app.main import app; print(sorted(r.path for r in app.routes))"`
Expected: prints a list including `/health`, `/operations`, `/ready`.

- [ ] **Step 7: Commit**

```bash
git add app/api/health.py app/main.py tests/integration/test_health.py
git commit -m "feat(api): /health + /ready endpoints and FastAPI app factory"
```

---

## Task 18: Ping self-check tool (`app/tools/ping.py`)

**Files:**
- Create: `app/tools/ping.py`
- Test: `tests/unit/test_ping.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ping.py
import httpx
import respx

from app.config import Settings
from app.tools.ping import run_ping


@respx.mock
async def test_ping_returns_0_on_200():
    settings = Settings(python_signing_secret="s", app_url="https://laravel.test")
    route = respx.post("https://laravel.test/webhooks/_ping").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    code = await run_ping(settings)
    assert code == 0 and route.called


@respx.mock
async def test_ping_returns_1_on_401():
    settings = Settings(python_signing_secret="s", app_url="https://laravel.test")
    respx.post("https://laravel.test/webhooks/_ping").mock(return_value=httpx.Response(401))
    assert await run_ping(settings) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_ping.py -v`
Expected: FAIL — cannot import `run_ping`.

- [ ] **Step 3: Write the implementation**

```python
# app/tools/ping.py
import asyncio
import json

import httpx

from app.config import Settings, get_settings
from app.security.hmac import sign


async def run_ping(settings: Settings) -> int:
    raw = json.dumps({"ping": True}, separators=(",", ":"))
    headers = sign(settings.python_signing_secret, raw)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(settings.ping_url, content=raw.encode(), headers=headers)
    print(f"ping {settings.ping_url} -> {resp.status_code} {resp.text}")
    return 0 if resp.status_code == 200 else 1


def main() -> int:
    return asyncio.run(run_ping(get_settings()))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_ping.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/tools/ping.py tests/unit/test_ping.py
git commit -m "feat(tools): /webhooks/_ping signing self-check (make ping)"
```

---

## Task 19: Docker (`docker/Dockerfile`, `docker/docker-compose.yml`, `docker/docker-compose.dev.yml`)

**Files:**
- Create: `docker/Dockerfile`, `docker/docker-compose.yml`, `docker/docker-compose.dev.yml`

- [ ] **Step 1: Write `docker/Dockerfile`**

```dockerfile
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

# API container overrides this; worker container sets its own command in compose.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
```

- [ ] **Step 2: Write `docker/docker-compose.yml` (production-ish)**

```yaml
services:
  api:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    command: uvicorn app.main:app --host 0.0.0.0 --port 8001
    env_file: ../.env
    ports:
      - "127.0.0.1:8001:8001"
    depends_on:
      - redis
    extra_hosts:
      - "host.docker.internal:host-gateway"
    restart: unless-stopped

  worker:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    command: arq app.worker.settings.WorkerSettings
    env_file: ../.env
    depends_on:
      - redis
    extra_hosts:
      - "host.docker.internal:host-gateway"
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    volumes:
      - redis-data:/data
    restart: unless-stopped

volumes:
  redis-data: {}
```

- [ ] **Step 3: Write `docker/docker-compose.dev.yml` (hot reload)**

```yaml
services:
  api:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    command: uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
    env_file: ../.env
    volumes:
      - ../app:/app/app
    ports:
      - "127.0.0.1:8001:8001"
    depends_on:
      - redis
    extra_hosts:
      - "host.docker.internal:host-gateway"

  worker:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    command: arq app.worker.settings.WorkerSettings --watch app
    env_file: ../.env
    volumes:
      - ../app:/app/app
    depends_on:
      - redis
    extra_hosts:
      - "host.docker.internal:host-gateway"

  redis:
    image: redis:7-alpine
    ports:
      - "127.0.0.1:6379:6379"
```

- [ ] **Step 4: Validate compose files parse**

Run: `docker compose -f docker/docker-compose.yml config >/dev/null && docker compose -f docker/docker-compose.dev.yml config >/dev/null && echo OK`
Expected: prints `OK` (requires Docker installed; if Docker is unavailable locally, skip and verify on the VPS).

- [ ] **Step 5: Commit**

```bash
git add docker/Dockerfile docker/docker-compose.yml docker/docker-compose.dev.yml
git commit -m "build: Docker image + dev/prod compose (api, worker, redis)"
```

---

## Task 20: Documentation (`README.md`, `docs/architecture.md`, `docs/runbook.md`, `CLAUDE.md`)

**Files:**
- Create: `README.md`, `docs/architecture.md`, `docs/runbook.md`, `CLAUDE.md`

- [ ] **Step 1: Write `README.md`**

````markdown
# Casino Game Service (Python)

Worker service that drives external game backends for the Laravel `casino-app`. It receives a
signed `POST /operations` trigger, acks `202`, runs the operation against the game backend on an
`arq`/Redis worker, and reports the result via a signed webhook. **Money/account tables are owned by
Laravel** — this service reads the shared MySQL and never writes money state.

Wire contract: `../laravel/casino-app/docs/integrations/python-game-service-api-contract.md`.

## Quick start (local, Docker)

```bash
cp .env.example .env        # set PYTHON_SIGNING_SECRET to match Laravel, APP_URL, DB_*, REDIS_URL
make up                     # api (:8001) + worker + redis
make ping                   # verify HMAC end-to-end against Laravel /webhooks/_ping -> 200
```

## Quick start (local, no Docker)

```bash
make install                # pip install -e ".[dev]"
make test                   # run the test suite
uvicorn app.main:app --reload --port 8001   # needs a reachable Redis + MySQL
arq app.worker.settings.WorkerSettings      # in a second shell
```

## Layout

See `docs/architecture.md`. Phase-1 scope and design: `docs/superpowers/specs/`.
````

- [ ] **Step 2: Write `docs/architecture.md`**

```markdown
# Architecture

## Components
- **api** (FastAPI/uvicorn) — verifies HMAC, parses/correlates, dedupes (arq job_id), enqueues, acks `202`.
- **worker** (arq) — runs `execute_operation`: pre-flight → backend → result validation → signed webhook.
- **redis** — arq queue (and, in later phases, session/rate-limit state).
- **MySQL** — shared with Laravel; read-only here.

## Request lifecycle
1. `POST /operations` (signed) → `verify_signature` → parse → `enqueue_job(job_id=idempotency_key)` → `202`.
2. Worker: `build_context` (games + game_accounts) → `get_backend` → `dispatch` by type → result model.
3. `deliver_webhook` (signed, backoff) → Laravel `{APP_URL}/webhooks/games/operation` until `200` / budget.

## Key modules
- `app/security/hmac.py` — the §1 signing scheme (raw-body exact).
- `app/schemas/` — §4 request union, §5 result models.
- `app/backends/` — `GameBackend` protocol, registry, MockBackend. New games add a module + registry entry.
- `app/operations/executor.py` — orchestration and webhook reporting.

## Adding a real backend (Phase 2+)
Implement `GameBackend` in `app/backends/<game>/backend.py`, map it in `app/backends/registry.py`,
read creds from `BackendContext.credentials`. Implement the Redis backend-result cache for
non-idempotent ops (RECHARGE/REDEEM) so a worker restart cannot double-apply.
```

- [ ] **Step 3: Write `docs/runbook.md`**

```markdown
# Runbook

## Health
- `GET /health` — liveness (always 200 if the process is up).
- `GET /ready` — checks MySQL + Redis; 503 if either is down.

## Verify signing against Laravel
`make ping` → expects `200 {"ok":true}` from `{APP_URL}/webhooks/_ping`. A `401` means the shared
secret or clock (NTP, 300s window) is wrong.

## Common failures (reported as webhook status:failed)
- `invalid_payload: ...` — trigger body failed §4 validation.
- `preflight_failed: game_not_found|game_account_not_found|missing_game_account_id` — DB lookups.
- `backend_error: ...` — the game backend call failed.

## Force a failure for testing
Set `MOCK_FORCE_FAIL=true` (and optional `MOCK_FORCE_FAIL_REASON`) and restart the worker.

## Webhook delivery
Retries on conn-error/5xx/404 with backoff up to `WEBHOOK_MAX_BUDGET_SECONDS` (default 600s).
`401`/`422` are sender bugs and are not retried — check signing / payload.
```

- [ ] **Step 4: Write `CLAUDE.md`**

```markdown
# CLAUDE.md — Casino Game Service (Python)

## What this is
Worker service driving external game backends for the Laravel `casino-app`. Receives a signed
`POST /operations`, acks `202`, runs work on an arq/Redis worker, and reports via a signed webhook.
Laravel owns all money/account writes; this service reads the shared MySQL only.

## Golden rules
- **Never write** money/account tables. Read-only DB access (`games`, `game_accounts`, `game_operations`).
- **Never log secrets**: `backend_password`, `api_secret_key`, `binding_key`, account/result `password`.
  Logging redaction is in `app/logging.py` (`SECRET_KEYS`); keep it current.
- HMAC must be byte-exact over the raw body; re-sign on every webhook retry (300s replay window).
- Always return `202` for a correlatable trigger; report real failures via the webhook (`status:"failed"`).
  Reserve non-`202` for bad signatures (401) and uncorrelatable bodies (400).

## Where things live
- Wire scheme: `app/security/hmac.py` · Schemas: `app/schemas/` · Backends: `app/backends/`
- Orchestration: `app/operations/executor.py` + `dispatch.py` · Worker: `app/worker/`
- API: `app/api/` · Config: `app/config.py`

## Workflow
- TDD: write the failing test first, then the minimal code (see `docs/superpowers/plans/`).
- Per feature: build → user tests manually → commit on approval.
- Integrating a new game backend: request the API-findings doc, confirm it covers success + error
  responses, then add a `GameBackend` module + registry entry.

## Commands
`make install` · `make test` · `make lint` · `make type` · `make up` · `make ping`

## Specs & plans
`docs/superpowers/specs/` (design) · `docs/superpowers/plans/` (implementation).
```

- [ ] **Step 5: Commit**

```bash
git add README.md docs/architecture.md docs/runbook.md CLAUDE.md
git commit -m "docs: README, architecture, runbook, and project CLAUDE.md"
```

---

## Task 21: Full-loop integration test + suite green

**Files:**
- Create: `tests/integration/test_full_loop.py`

- [ ] **Step 1: Write the full-loop test (api → enqueue → executor → webhook)**

```python
# tests/integration/test_full_loop.py
import json

import httpx
import respx

from app.config import Settings, get_settings
from app.operations.executor import execute_operation
from app.security.hmac import sign

WEBHOOK = "https://laravel.test/webhooks/games/operation"


class CapturingArq:
    """Stands in for the arq pool: records jobs, then runs the executor inline."""

    def __init__(self, seeded):
        self.seeded = seeded
        self.enqueued = []

    async def enqueue_job(self, func, payload, _job_id=None):
        self.enqueued.append((func, payload, _job_id))


@respx.mock
async def test_create_account_round_trip(monkeypatch, seeded):
    monkeypatch.setenv("PYTHON_SIGNING_SECRET", "s")
    monkeypatch.setenv("APP_URL", "https://laravel.test")
    get_settings.cache_clear()

    from app.api.operations import router
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    arq = CapturingArq(seeded)
    app.state.arq = arq

    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))

    body = json.dumps(
        {"idempotency_key": "loop-1", "type": "CREATE_ACCOUNT", "user_id": 42, "game_id": 7, "game_account_id": None},
        separators=(",", ":"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/operations", content=body, headers=sign("s", body))
    assert resp.status_code == 202
    assert arq.enqueued[0][2] == "loop-1"

    # Simulate the worker picking up the enqueued job:
    _, payload, _ = arq.enqueued[0]
    settings = Settings(python_signing_secret="s", app_url="https://laravel.test")
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=settings)

    sent = json.loads(route.calls.last.request.content.decode())
    assert sent["idempotency_key"] == "loop-1"
    assert sent["status"] == "succeeded"
    assert sent["result"]["username"] == "mock_42_7"
    get_settings.cache_clear()
```

- [ ] **Step 2: Run the full suite**

Run: `pytest -q`
Expected: PASS — all tests across every task green.

- [ ] **Step 3: Lint + type-check**

Run: `ruff check app tests && mypy app`
Expected: ruff clean; mypy clean (fix any reported issues inline).

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_full_loop.py
git commit -m "test: full request->202->executor->signed webhook loop"
```

---

## Phase-1 acceptance (manual, after the suite is green)

Performed by the user against the real Laravel locally (see spec §15):

1. `make up` (or `docker compose -f docker/docker-compose.dev.yml up --build`); `.env` has the matching
   `PYTHON_SIGNING_SECRET`, `APP_URL` → local Laravel, `DB_*` → shared MySQL, `REDIS_URL`.
2. `make ping` → `200 {"ok":true}` (HMAC verified end-to-end).
3. Point Laravel `PYTHON_BASE_URL` at `http://127.0.0.1:8001`.
4. Trigger CREATE_ACCOUNT / READ_BALANCE from Laravel → operation finalizes to **SUCCEEDED** with
   MockBackend data.
5. Set `MOCK_FORCE_FAIL=true`, restart worker, trigger again → operation **FAILED** + refund.
6. Send a duplicate trigger (same `idempotency_key`) → exactly one job runs.

---

## Self-review (completed by plan author)

**Spec coverage:** §4 transport/HMAC → Task 3; §4 request payloads → Task 4; §5 results → Task 5;
read schema (§8) → Tasks 6–7; backend abstraction (§6 spec) → Tasks 8–10; pre-flight → Task 11;
post-flight seam → Task 12; webhook + retry (§7.3) → Task 13; lifecycle/executor (§10/§11) → Task 14;
worker (Redis/arq) → Task 15; `/operations` + dedupe + D5 handling → Task 16; health + app → Task 17;
ping self-check → Task 18; Docker (§17) → Task 19; docs + CLAUDE.md → Task 20; logging (§12) →
Task 2 + bound logs in Tasks 13/14/16; tests (§16) → throughout + Task 21; config (§13) → Task 1.
AGENT_BALANCE handler is covered by Tasks 4/9/14 (wire-ready; Laravel side pending per D1). The
backend-result Redis cache is a **seam** (deferred per D6) — no Phase-1 task implements the cache,
which matches the spec.

**Placeholder scan:** No TBD/TODO; every code step shows complete, runnable code.

**Type consistency:** `BackendContext(credentials, user_id, account)`, `GameCredentials`,
`AccountIdentity`, result model names, `deliver_webhook`/`WebhookResult`, `build_context`,
`execute_operation`, `dispatch`, `execute_operation_task`, `verify_signature`, `get_backend`,
`apply_post_effects` are used consistently across tasks.
```
