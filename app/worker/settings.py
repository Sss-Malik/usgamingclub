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
