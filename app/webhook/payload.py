# app/webhook/payload.py
import time

from app.operations.result_cache import CachedOutcome
from app.schemas.requests import Operation

GENERIC_MESSAGE = "Something went wrong. Please try again later."


def _status(outcome: CachedOutcome) -> str:
    return {"succeeded": "success", "failed": "failed", "error": "error"}.get(
        outcome.status, "error"
    )


def _message(outcome: CachedOutcome) -> str:
    if outcome.status == "succeeded":
        return ""
    if outcome.status == "error":
        return GENERIC_MESSAGE
    reason = outcome.reason or "failed"
    # Surface provider/business text without the internal prefix.
    for prefix in ("backend_error: ", "preflight_failed: ", "invalid_payload: ",
                   "invalid_result_payload: "):
        if reason.startswith(prefix):
            reason = reason[len(prefix):]
            break
    return reason or GENERIC_MESSAGE


def build_webhook_payload(
    op: Operation, outcome: CachedOutcome, *, backend_id: int | None
) -> dict:
    status = _status(outcome)
    body: dict = {
        "action": op.action,
        "status": status,
        "message": _message(outcome),
        "timestamp": int(time.time()),
        "user_id": op.user_id,
        "backend_name": op.backend_name,
    }
    if backend_id is not None:
        body["backend_id"] = backend_id

    # Correlation ids are always echoed so Arcadia can resolve the local row.
    body.update(op.correlation)

    # Money ops echo the original (whole-dollar) amount for Arcadia's amount-verification.
    if op.action in ("recharge", "redeem", "freeplay") and op.amount is not None:
        body["amount"] = op.amount

    if status != "success":
        return body

    result = outcome.result or {}
    if op.action == "create":
        body["account_created"] = [{
            "username": result.get("username"),
            "password": result.get("password"),
            "id_from_backend": result.get("external_user_id"),
        }]
    elif op.action == "reset_password":
        body["new_password"] = result.get("password")
    elif op.action == "read":
        body["user_data"] = {"balance": result.get("balance")}
    return body
