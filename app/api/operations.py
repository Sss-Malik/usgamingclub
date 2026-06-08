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

    # Per-driver retry policy: peek the game's driver to decide arq's _max_tries.
    # Default (3) is safe for idempotent drivers (GameVault family). Non-idempotent
    # drivers (gameroom) get _max_tries=1 so a worker crash can't double-apply funds.
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

    try:
        await request.app.state.arq.enqueue_job(
            "execute_operation_task", data, _job_id=key, _max_tries=max_tries,
        )
    except Exception:  # noqa: BLE001 - any enqueue failure must surface as a non-202
        logger.exception("operation_enqueue_failed", idempotency_key=key, phase="enqueued")
        return Response(status_code=500)
    logger.bind(idempotency_key=key, phase="enqueued").info("operation_enqueued", max_tries=max_tries)
    return Response(status_code=202)
