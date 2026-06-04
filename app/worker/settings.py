# app/worker/settings.py
import httpx
from arq.connections import RedisSettings

from app.config import get_settings, require_runtime_settings
from app.db.engine import get_sessionmaker
from app.logging import configure_logging
from app.worker.tasks import execute_operation_task


async def startup(ctx: dict) -> None:
    configure_logging()
    require_runtime_settings(get_settings())
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
    # Backstop for worker crashes. Safe in Phase 1 because MockBackend is idempotent.
    # WARNING (Phase 2): before wiring any real, non-idempotent money backend
    # (RECHARGE/REDEEM), implement the Redis backend-result cache (spec D6) OR set
    # max_tries = 1 — otherwise a crash mid-op re-runs the backend call and double-applies funds.
    max_tries = 3
    keep_result = 0
