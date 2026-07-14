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
        account_username=generate_username(req.full_name, provided=req.username),
        op_id=req.op_id,
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
        op_id=req.op_id,
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
        op_id=req.op_id,
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
        op_id=req.op_id,
    )
    return await _enqueue(request, op)


@router.post("/freeplay")
async def freeplay(request: Request, raw: bytes = Depends(verify_request_signature)) -> Response:
    req = FreeplayRequest.model_validate(json.loads(raw))
    # Key on the unique per-attempt id (game_recharge row) when Arcadia sends it, so retries of
    # the same freeplay across games are distinct ops; fall back to freeplay_id otherwise.
    correlation: dict[str, str | int] = {"freeplay_id": req.freeplay_id}
    if req.freeplay_recharge_id is not None:
        correlation["freeplay_recharge_id"] = req.freeplay_recharge_id
    corr_id = req.freeplay_recharge_id if req.freeplay_recharge_id is not None else req.freeplay_id
    op = Operation(
        action="freeplay", type="FREEPLAY",
        idempotency_key=f"freeplay:{corr_id}",
        user_id=req.user_id, backend_name=req.backend_name, username=req.username,
        amount=req.amount, correlation=correlation,
        op_id=req.op_id,
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
        op_id=req.op_id,
    )
    return await _enqueue(request, op)
