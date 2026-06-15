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
    if op.type in ("RECHARGE", "FREEPLAY"):
        # Freeplay is an additive credit — same backend op as recharge.
        return await backend.recharge(ctx, amount=int(op.amount or 0))
    if op.type == "REDEEM":
        return await backend.redeem(ctx, amount=int(op.amount or 0))
    raise BackendError(f"unsupported_type: {op.type}")
