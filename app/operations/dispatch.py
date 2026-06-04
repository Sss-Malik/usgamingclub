# app/operations/dispatch.py
from pydantic import BaseModel

from app.backends.base import BackendError, GameBackend
from app.backends.context import BackendContext


async def dispatch(backend: GameBackend, op, ctx: BackendContext) -> BaseModel:
    if op.type == "CREATE_ACCOUNT":
        return await backend.create_account(ctx)
    if op.type == "READ_BALANCE":
        return await backend.read_balance(ctx)
    if op.type == "RESET_PASSWORD":
        return await backend.reset_password(ctx)
    if op.type == "RECHARGE":
        return await backend.recharge(
            ctx,
            amount_cents=op.amount_cents,
            bonus_cents=op.bonus_cents,
            total_credit_cents=op.total_credit_cents,
        )
    if op.type == "REDEEM":
        return await backend.redeem(ctx, amount_cents=op.amount_cents)
    if op.type == "AGENT_BALANCE":
        return await backend.agent_balance(ctx)
    raise BackendError(f"unsupported_type: {op.type}")
