# app/worker/tasks.py
from app.config import get_settings
from app.operations.executor import execute_operation


async def execute_operation_task(ctx: dict, payload: dict) -> None:
    await execute_operation(
        payload,
        session_factory=ctx["session_factory"],
        http_client=ctx["http_client"],
        settings=get_settings(),
        result_cache=ctx["result_cache"],
    )
