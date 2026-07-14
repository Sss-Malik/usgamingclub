# app/operations/executor.py
import time

import httpx
from pydantic import BaseModel, ValidationError

from app.backends.base import BackendError, GameBackend, TransientBackendError
from app.backends.context import BackendContext
from app.backends.diagnostics import DiagnosticsRecorder
from app.backends.registry import resolve_backend as _resolve_backend
from app.config import Settings
from app.logging import get_logger
from app.operations.dispatch import dispatch
from app.operations.result_cache import CachedOutcome, InMemoryResultCache, ResultCache
from app.postflight.effects import apply_post_effects
from app.preflight.checks import PreflightError, build_context
from app.schemas.requests import Operation
from app.webhook.client import deliver_webhook
from app.webhook.payload import assemble_diagnostics, build_webhook_payload

logger = get_logger(__name__)

_REASON_PREFIXES = ("backend_error: ", "preflight_failed: ", "invalid_payload: ",
                    "invalid_result_payload: ", "retry_blocked: ")


def _public_reason(reason: str | None) -> str | None:
    """The REAL reason for diagnostics — strip the internal stage prefix, keep the meat."""
    if reason is None:
        return None
    for prefix in _REASON_PREFIXES:
        if reason.startswith(prefix):
            return reason[len(prefix):]
    return reason


def _provider_from_exc(exc: BackendError) -> dict | None:
    provider = {"http_status": exc.provider_http_status,
                "code": exc.provider_code,
                "message": exc.provider_message}
    return provider if any(v is not None for v in provider.values()) else None


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
    attempt: int = 1,
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
    recorder = DiagnosticsRecorder()
    started = time.monotonic()

    async def deliver(outcome, *, backend_id, failure_kind=None, provider=None,
                      cache_hit=False, snapshot=None):
        # Diagnostics assembly is observational only — a bug in it must NEVER suppress the
        # webhook. Isolate it behind its own try/except; on failure fall back to
        # diagnostics=None (build_webhook_payload + deliver_webhook always run below).
        try:
            duration_ms = int((time.monotonic() - started) * 1000)
            snap = snapshot if snapshot is not None else recorder.snapshot()
            reason = _public_reason(outcome.reason) if failure_kind else None
            diagnostics = assemble_diagnostics(
                op_id=op.op_id, idempotency_key=key, attempt=attempt, cache_hit=cache_hit,
                duration_ms=duration_ms, snapshot=snap, failure_kind=failure_kind,
                reason=reason, provider=provider,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("operation_diagnostics_assembly_failed", error=str(exc))
            diagnostics = None
        body = build_webhook_payload(op, outcome, backend_id=backend_id, diagnostics=diagnostics)
        await deliver_webhook(
            http_client, settings.webhook_url, settings.webhook_secret, body,
            max_budget_seconds=settings.webhook_max_budget_seconds,
            backoff_base=settings.webhook_backoff_base, backoff_max=settings.webhook_backoff_max,
        )

    # 0. Retry blocked: a non-idempotent op is being re-run after a crash. Report `error`
    # so Arcadia finalizes in seconds; the backend is NOT called.
    if retry_blocked:
        outcome = CachedOutcome("error", None, "retry_blocked: manual reconcile may be required")
        log.warning("operation_retry_blocked")
        await deliver(outcome, backend_id=None, failure_kind="retry_blocked")
        return

    # 2. Replay short-circuit.
    cached = await result_cache.get(key)
    if cached is not None:
        log.bind(phase="cache_hit").info("operation_replay_from_cache", status=cached.status)
        detail = cached.detail or {}
        replay_snapshot: dict = {
            "steps": [], "session_reuse": None,
            "external_user_id": detail.get("external_user_id"),
            "balance_before": detail.get("balance_before"),
            "balance_after": detail.get("balance_after"),
        }
        await deliver(cached, backend_id=None, cache_hit=True,
                      failure_kind=detail.get("failure_kind"),
                      provider=detail.get("provider"), snapshot=replay_snapshot)
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
                diagnostics=recorder,
                op_id=op.op_id,
                attempt=attempt,
            )
    except PreflightError as exc:
        await deliver(CachedOutcome("failed", None, f"preflight_failed: {exc.reason}"),
                      backend_id=None, failure_kind="preflight")
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
            diagnostics=recorder,
        )
    except BackendError as exc:
        await deliver(CachedOutcome("failed", None, exc.reason),
                      backend_id=backend_id, failure_kind="preflight",
                      provider=_provider_from_exc(exc))
        return

    # 5. Backend call.
    log = log.bind(phase="backend_call", backend_id=backend_id)
    try:
        result: BaseModel = await dispatch(backend, op, ctx)
    except TransientBackendError as exc:
        log.warning("operation_backend_transient", reason=exc.reason)
        await deliver(CachedOutcome("error", None, f"backend_error: {exc.reason}"),
                      backend_id=backend_id, failure_kind="transient",
                      provider=_provider_from_exc(exc))
        # Terminal for this job: execute_operation_task never raises, so arq does NOT re-run
        # on a transient error — it only re-runs after a worker crash/job loss (and then
        # `retry_blocked` caps non-idempotent drivers at one attempt). Arcadia therefore
        # treats this `error` webhook as final and hands the freeplay/recharge back to the
        # player. Deliberately NOT cached, so a crash re-run may retry an idempotent driver.
        return
    except BackendError as exc:
        provider = _provider_from_exc(exc)
        snap = recorder.snapshot()
        detail = {"failure_kind": "backend", "provider": provider,
                  "external_user_id": snap.get("external_user_id"),
                  "balance_before": snap.get("balance_before"),
                  "balance_after": snap.get("balance_after")}
        outcome = CachedOutcome("failed", None, f"backend_error: {exc.reason}", detail=detail)
        await result_cache.set(key, outcome, settings.result_cache_ttl_seconds)
        log.warning("operation_backend_failed", reason=exc.reason)
        await deliver(outcome, backend_id=backend_id, failure_kind="backend", provider=provider)
        return
    except ValidationError as exc:
        snap = recorder.snapshot()
        detail = {"failure_kind": "invalid_result", "provider": None,
                  "external_user_id": snap.get("external_user_id"),
                  "balance_before": snap.get("balance_before"),
                  "balance_after": snap.get("balance_after")}
        outcome = CachedOutcome("failed", None, f"invalid_result_payload: {_summarize(exc)}",
                                detail=detail)
        await result_cache.set(key, outcome, settings.result_cache_ttl_seconds)
        log.error("operation_invalid_result", reason=outcome.reason)
        await deliver(outcome, backend_id=backend_id, failure_kind="invalid_result", snapshot=snap)
        return
    except Exception:  # noqa: BLE001
        log.exception("operation_unexpected_error")
        await deliver(CachedOutcome("error", None, "backend_error: unexpected"),
                      backend_id=backend_id, failure_kind="unexpected")
        return

    snap = recorder.snapshot()
    detail = {"failure_kind": None, "provider": None,
              "external_user_id": snap.get("external_user_id"),
              "balance_before": snap.get("balance_before"),
              "balance_after": snap.get("balance_after")}
    outcome = CachedOutcome("succeeded", result.model_dump(exclude_none=True), None, detail=detail)
    await result_cache.set(key, outcome, settings.result_cache_ttl_seconds)
    log.bind(phase="backend_result").info("operation_succeeded")
    await deliver(outcome, backend_id=backend_id, snapshot=snap)
    await apply_post_effects(key, op.type, outcome.result or {})


def _summarize(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "validation error"
    first = errors[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    return f"{loc}: {first.get('msg', 'invalid')}"
