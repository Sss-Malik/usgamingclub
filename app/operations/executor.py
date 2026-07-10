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
from app.schemas.requests import Operation
from app.webhook.client import deliver_webhook
from app.webhook.payload import build_webhook_payload

logger = get_logger(__name__)


async def execute_operation(
    payload: dict,
    *,
    session_factory,
    http_client: httpx.AsyncClient,
    settings: Settings,
    result_cache: ResultCache | None = None,
    session_store=None,
    redis=None,
    retry_blocked: bool = False,
    resolve=_resolve_backend,
) -> None:
    if result_cache is None:
        result_cache = InMemoryResultCache()

    # 1. Parse the normalized op (invalid payloads cannot be correlated → log + drop).
    try:
        op = Operation.model_validate(payload)
    except ValidationError as exc:
        logger.error("operation_unparseable_op", error=_summarize(exc))
        return

    key = op.idempotency_key
    log = logger.bind(idempotency_key=key, action=op.action, type=op.type)

    # 0. Retry blocked: a non-idempotent op is being re-run after a crash. Report `error`
    # so Arcadia finalizes in seconds; the backend is NOT called.
    if retry_blocked:
        outcome = CachedOutcome("error", None, "retry_blocked: manual reconcile may be required")
        log.warning("operation_retry_blocked")
        await _deliver(http_client, settings, op, outcome, backend_id=None)
        return

    # 2. Replay short-circuit.
    cached = await result_cache.get(key)
    if cached is not None:
        log.bind(phase="cache_hit").info("operation_replay_from_cache", status=cached.status)
        await _deliver(http_client, settings, op, cached, backend_id=None)
        return

    # 3. Pre-flight (failures reported, not cached).
    try:
        async with session_factory() as session:
            ctx: BackendContext = await build_context(
                session,
                type=op.type,
                backend_name=op.backend_name,
                username=op.username,
                user_id=op.user_id,
                idempotency_key=key,
                account_username=op.account_username,
            )
    except PreflightError as exc:
        await _deliver(http_client, settings, op,
                       CachedOutcome("failed", None, f"preflight_failed: {exc.reason}"),
                       backend_id=None)
        return

    backend_id = ctx.credentials.game_id

    # 4. Resolve backend (config error → failure, not cached).
    try:
        backend: GameBackend = resolve(
            ctx.credentials.backend_driver,
            credentials=ctx.credentials,
            http_client=http_client,
            settings=settings,
            session_store=session_store,
            redis=redis,
        )
    except BackendError as exc:
        await _deliver(http_client, settings, op,
                       CachedOutcome("failed", None, exc.reason), backend_id=backend_id)
        return

    # 5. Backend call.
    log = log.bind(phase="backend_call", backend_id=backend_id)
    try:
        result: BaseModel = await dispatch(backend, op, ctx)
    except TransientBackendError as exc:
        log.warning("operation_backend_transient", reason=exc.reason)
        await _deliver(http_client, settings, op,
                       CachedOutcome("error", None, f"backend_error: {exc.reason}"),
                       backend_id=backend_id)
        # Terminal for this job: execute_operation_task never raises, so arq does NOT re-run
        # on a transient error — it only re-runs after a worker crash/job loss (and then
        # `retry_blocked` caps non-idempotent drivers at one attempt). Arcadia therefore
        # treats this `error` webhook as final and hands the freeplay/recharge back to the
        # player. Deliberately NOT cached, so a crash re-run may retry an idempotent driver.
        return
    except BackendError as exc:
        outcome = CachedOutcome("failed", None, f"backend_error: {exc.reason}")
        await result_cache.set(key, outcome, settings.result_cache_ttl_seconds)
        log.warning("operation_backend_failed", reason=exc.reason)
        await _deliver(http_client, settings, op, outcome, backend_id=backend_id)
        return
    except ValidationError as exc:
        outcome = CachedOutcome("failed", None, f"invalid_result_payload: {_summarize(exc)}")
        await result_cache.set(key, outcome, settings.result_cache_ttl_seconds)
        log.error("operation_invalid_result", reason=outcome.reason)
        await _deliver(http_client, settings, op, outcome, backend_id=backend_id)
        return
    except Exception:  # noqa: BLE001
        log.exception("operation_unexpected_error")
        await _deliver(http_client, settings, op,
                       CachedOutcome("error", None, "backend_error: unexpected"),
                       backend_id=backend_id)
        return

    outcome = CachedOutcome("succeeded", result.model_dump(exclude_none=True), None)
    await result_cache.set(key, outcome, settings.result_cache_ttl_seconds)
    log.bind(phase="backend_result").info("operation_succeeded")
    await _deliver(http_client, settings, op, outcome, backend_id=backend_id)
    await apply_post_effects(key, op.type, outcome.result or {})


async def _deliver(client, settings: Settings, op: Operation, outcome: CachedOutcome,
                   *, backend_id: int | None) -> None:
    body = build_webhook_payload(op, outcome, backend_id=backend_id)
    await deliver_webhook(
        client,
        settings.webhook_url,
        settings.webhook_secret,
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
