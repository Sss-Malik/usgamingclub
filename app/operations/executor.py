# app/operations/executor.py
import httpx
from pydantic import BaseModel, ValidationError

from app.backends.base import BackendError, GameBackend, TransientBackendError
from app.backends.context import BackendContext
from app.backends.registry import resolve_backend as _resolve_backend
from app.config import Settings
from app.logging import get_logger
from app.operations.dispatch import dispatch
from app.operations.result_cache import CachedOutcome, InMemoryResultCache, ResultCache
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
    result_cache: ResultCache | None = None,
    resolve=_resolve_backend,
) -> None:
    if result_cache is None:
        result_cache = InMemoryResultCache()
    key = str(payload.get("idempotency_key", ""))
    log = logger.bind(idempotency_key=key, phase="received")

    # 1. Validate (invalid payloads are reported, never cached).
    try:
        op = operation_adapter.validate_python(payload)
    except ValidationError as exc:
        await _deliver(http_client, settings, key, CachedOutcome("failed", None, f"invalid_payload: {_summarize(exc)}"))
        return

    log = log.bind(type=op.type, game_id=op.game_id)

    # 2. Replay short-circuit.
    cached = await result_cache.get(key)
    if cached is not None:
        log.bind(phase="cache_hit").info("operation_replay_from_cache", status=cached.status)
        # NOTE: apply_post_effects is intentionally skipped on replay. It is a no-op today; if it
        # gains real behavior in Phase 3, revisit whether replays must run it.
        await _deliver(http_client, settings, key, cached)
        return

    # 3. Pre-flight (not cached on failure).
    try:
        async with session_factory() as session:
            ctx: BackendContext = await build_context(
                session,
                type=op.type,
                game_id=op.game_id,
                game_account_id=getattr(op, "game_account_id", None),
                user_id=getattr(op, "user_id", None),
                idempotency_key=key,
                account_username=getattr(op, "account_username", None),
            )
    except PreflightError as exc:
        await _deliver(http_client, settings, key, CachedOutcome("failed", None, f"preflight_failed: {exc.reason}"))
        return

    # 4. Resolve backend (config error -> failure, not cached).
    try:
        backend: GameBackend = resolve(
            ctx.credentials.backend_driver, credentials=ctx.credentials, http_client=http_client, settings=settings
        )
    except BackendError as exc:
        await _deliver(http_client, settings, key, CachedOutcome("failed", None, exc.reason))
        return

    # 5. Backend call.
    log = log.bind(phase="backend_call")
    try:
        result: BaseModel = await dispatch(backend, op, ctx)
    except TransientBackendError as exc:
        log.warning("operation_backend_transient", reason=exc.reason)
        await _deliver(http_client, settings, key, CachedOutcome("failed", None, f"backend_error: {exc.reason}"))
        return  # not cached -> arq re-run retries (order_id dedupe keeps money ops safe)
    except BackendError as exc:
        outcome = CachedOutcome("failed", None, f"backend_error: {exc.reason}")
        await result_cache.set(key, outcome, settings.result_cache_ttl_seconds)
        log.warning("operation_backend_failed", reason=exc.reason)
        await _deliver(http_client, settings, key, outcome)
        return
    except ValidationError as exc:
        # A malformed backend result is terminal (same inputs -> same error); cache it so a worker
        # re-run does not re-call the backend (money-op safety for recharge/redeem).
        outcome = CachedOutcome("failed", None, f"invalid_result_payload: {_summarize(exc)}")
        await result_cache.set(key, outcome, settings.result_cache_ttl_seconds)
        log.error("operation_invalid_result", reason=outcome.reason)
        await _deliver(http_client, settings, key, outcome)
        return
    except Exception:  # noqa: BLE001 - any unexpected error is reported, not cached
        log.exception("operation_unexpected_error")
        await _deliver(http_client, settings, key, CachedOutcome("failed", None, "backend_error: unexpected"))
        return

    outcome = CachedOutcome("succeeded", result.model_dump(exclude_none=True), None)
    await result_cache.set(key, outcome, settings.result_cache_ttl_seconds)
    log.bind(phase="backend_result").info("operation_succeeded", result_keys=sorted((outcome.result or {}).keys()))
    await _deliver(http_client, settings, key, outcome)
    await apply_post_effects(key, op.type, outcome.result or {})


async def _deliver(client, settings: Settings, key: str, outcome: CachedOutcome) -> None:
    if outcome.status == "succeeded":
        body = {"idempotency_key": key, "status": "succeeded", "result": outcome.result or {}}
    else:
        body = {"idempotency_key": key, "status": "failed", "reason": (outcome.reason or "failed")[:255]}
    await deliver_webhook(
        client,
        settings.webhook_url,
        settings.python_signing_secret,
        body,
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
