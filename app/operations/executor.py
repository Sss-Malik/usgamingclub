# app/operations/executor.py
import httpx
from pydantic import BaseModel, ValidationError

from app.backends.base import BackendError, GameBackend
from app.backends.context import BackendContext
from app.backends.registry import get_backend
from app.config import Settings
from app.logging import get_logger
from app.operations.dispatch import dispatch
from app.postflight.effects import apply_post_effects
from app.preflight.checks import PreflightError, build_context
from app.schemas.operations import operation_adapter
from app.webhook.client import deliver_webhook

logger = get_logger(__name__)


async def execute_operation(
    payload: dict,
    *,
    session_factory,
    http_client: httpx.AsyncClient,
    settings: Settings,
    backend_resolver=get_backend,
) -> None:
    key = str(payload.get("idempotency_key", ""))
    log = logger.bind(idempotency_key=key, phase="received")

    try:
        op = operation_adapter.validate_python(payload)
    except ValidationError as exc:
        reason = f"invalid_payload: {_summarize(exc)}"
        log.warning("operation_invalid", reason=reason)
        await _report_failure(http_client, settings, key, reason)
        return

    log = log.bind(type=op.type, game_id=op.game_id, phase="preflight")
    try:
        async with session_factory() as session:
            ctx: BackendContext = await build_context(
                session,
                type=op.type,
                user_id=getattr(op, "user_id", None),
                game_id=op.game_id,
                game_account_id=getattr(op, "game_account_id", None),
                idempotency_key=key,
                account_username=getattr(op, "account_username", None),
            )
    except PreflightError as exc:
        reason = f"preflight_failed: {exc.reason}"
        log.warning("operation_preflight_failed", reason=reason)
        await _report_failure(http_client, settings, key, reason)
        return

    backend: GameBackend = backend_resolver(op.game_id)
    log = log.bind(phase="backend_call")
    try:
        result: BaseModel = await dispatch(backend, op, ctx)
    except BackendError as exc:
        reason = f"backend_error: {exc.reason}"
        log.warning("operation_backend_failed", reason=reason)
        await _report_failure(http_client, settings, key, reason)
        return
    except ValidationError as exc:
        reason = f"invalid_result_payload: {_summarize(exc)}"
        log.error("operation_invalid_result", reason=reason)
        await _report_failure(http_client, settings, key, reason)
        return
    except Exception:  # noqa: BLE001 - any unexpected error becomes a reported failure
        log.exception("operation_unexpected_error")
        await _report_failure(http_client, settings, key, "backend_error: unexpected")
        return

    result_payload = result.model_dump(exclude_none=True)
    log.bind(phase="backend_result").info(
        "operation_succeeded", result_keys=sorted(result_payload.keys())
    )
    await _report_success(http_client, settings, key, result_payload)
    await apply_post_effects(key, op.type, result_payload)


async def _report_success(client, settings: Settings, key: str, result_payload: dict) -> None:
    await deliver_webhook(
        client,
        settings.webhook_url,
        settings.python_signing_secret,
        {"idempotency_key": key, "status": "succeeded", "result": result_payload},
        max_budget_seconds=settings.webhook_max_budget_seconds,
        backoff_base=settings.webhook_backoff_base,
        backoff_max=settings.webhook_backoff_max,
    )


async def _report_failure(client, settings: Settings, key: str, reason: str) -> None:
    await deliver_webhook(
        client,
        settings.webhook_url,
        settings.python_signing_secret,
        {"idempotency_key": key, "status": "failed", "reason": reason[:255]},
        max_budget_seconds=settings.webhook_max_budget_seconds,
        backoff_base=settings.webhook_backoff_base,
        backoff_max=settings.webhook_backoff_max,
    )


def _summarize(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "validation error"
    first = errors[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    return f"{loc}: {first.get('msg', 'invalid')}"
