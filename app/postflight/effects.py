"""Post-flight side-effect seam.

Per the contract, Laravel applies all money/account side effects when it processes the
webhook callback. Python writes nothing. This hook exists so future phases have a place
to record read-only telemetry; in Phase 1 it is intentionally a no-op.
"""


async def apply_post_effects(idempotency_key: str, op_type: str, result_payload: dict) -> None:
    return None
