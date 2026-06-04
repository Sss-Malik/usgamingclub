# app/worker/settings.py
import httpx
import redis.asyncio as redis_asyncio
from arq.connections import RedisSettings

from app.config import get_settings, require_runtime_settings
from app.db.engine import get_sessionmaker
from app.logging import configure_logging
from app.operations.result_cache import RedisResultCache
from app.worker.tasks import execute_operation_task


async def startup(ctx: dict) -> None:
    configure_logging()
    settings = get_settings()
    require_runtime_settings(settings)
    ctx["http_client"] = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    ctx["session_factory"] = get_sessionmaker()
    ctx["redis_cache"] = redis_asyncio.from_url(settings.redis_url)
    ctx["result_cache"] = RedisResultCache(ctx["redis_cache"])


async def shutdown(ctx: dict) -> None:
    await ctx["http_client"].aclose()
    await ctx["redis_cache"].aclose()


class WorkerSettings:
    functions = [execute_operation_task]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # Job timeout must exceed the webhook retry budget so a still-retrying job is not killed.
    job_timeout = int(get_settings().webhook_max_budget_seconds) + 60
    # Backstop for worker crashes. The result cache makes re-runs safe (cached terminal outcomes are
    # replayed without re-calling the backend); transient failures are re-tried and GameVault dedupes
    # by order_id, so money ops cannot double-apply.
    max_tries = 3
    keep_result = 0
