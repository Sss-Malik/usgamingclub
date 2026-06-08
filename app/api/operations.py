# app/api/operations.py
import json

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import verify_signature
from app.backends.registry import NON_IDEMPOTENT_DRIVERS
from app.db.repositories import GamesRepository
from app.logging import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.post("/operations")
async def receive_operation(
    request: Request, raw: bytes = Depends(verify_signature)
) -> Response:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("operation_unparseable_body", phase="received")
        return Response(status_code=400)

    key = data.get("idempotency_key") if isinstance(data, dict) else None
    if not isinstance(key, str) or key == "":
        logger.warning("operation_missing_idempotency_key", phase="received")
        return Response(status_code=400)

    # Per-driver retry policy. arq has NO _max_tries kwarg on enqueue_job (only _job_id /
    # _queue_name / _defer_* / _expires / _job_try). Any other _* kwarg is forwarded to the task
    # function and crashes with TypeError. So we embed the retry limit INSIDE the payload dict;
    # the worker task reads it and uses ctx["job_try"] to short-circuit retries for non-idempotent
    # drivers (gameroom, goldentreasure). Idempotent drivers (GameVault family) skip this and use
    # the worker's default max_tries=3 for transient resilience.
    max_tries: int | None = None
    game_id = data.get("game_id") if isinstance(data, dict) else None
    if isinstance(game_id, int):
        try:
            session_factory = getattr(request.app.state, "session_factory", None)
            if session_factory is not None:
                async with session_factory() as session:
                    driver = await GamesRepository(session).get_driver(game_id)
                if driver and driver.lower() in NON_IDEMPOTENT_DRIVERS:
                    max_tries = 1
        except Exception:  # noqa: BLE001 - DB blip: fall back to default; preflight surfaces the real error
            logger.exception("driver_peek_failed", idempotency_key=key, phase="received")

    if max_tries is not None:
        data = {**data, "_max_tries": max_tries}

    try:
        await request.app.state.arq.enqueue_job(
            "execute_operation_task", data, _job_id=key,
        )
    except Exception:  # noqa: BLE001 - any enqueue failure must surface as a non-202
        logger.exception("operation_enqueue_failed", idempotency_key=key, phase="enqueued")
        return Response(status_code=500)
    logger.bind(idempotency_key=key, phase="enqueued").info("operation_enqueued", max_tries=max_tries)
    return Response(status_code=202)
