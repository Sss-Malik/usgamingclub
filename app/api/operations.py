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
