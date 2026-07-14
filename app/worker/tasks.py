# app/worker/tasks.py
from app.config import get_settings
from app.operations.executor import execute_operation


async def execute_operation_task(ctx: dict, payload: dict) -> None:
    # Per-job retry limit: the API endpoint embeds `_max_tries` in the payload for non-idempotent
    # drivers (gameroom, goldentreasure). If arq is RE-RUNNING this op (ctx.job_try > _max_tries),
    # short-circuit before calling the backend — the previous attempt may have applied the change.
    max_tries = payload.get("_max_tries")
    job_try = ctx.get("job_try", 1) or 1
    retry_blocked = isinstance(max_tries, int) and job_try > max_tries
    await execute_operation(
        payload,
        session_factory=ctx["session_factory"],
        http_client=ctx["http_client"],
        settings=get_settings(),
        result_cache=ctx["result_cache"],
        session_store=ctx["session_store"],
        redis=ctx["redis_cache"],
        retry_blocked=retry_blocked,
        attempt=job_try,
    )
