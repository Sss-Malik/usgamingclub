# app/backends/base.py
from typing import Protocol

from app.backends.context import BackendContext
from app.schemas.results import (
    AgentBalanceResult,
    CreateAccountResult,
    ReadBalanceResult,
    RechargeResult,
    RedeemResult,
    ResetPasswordResult,
)


class BackendError(Exception):
    """Raised when a game backend call fails in a way that should be reported as status:failed."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class GameBackend(Protocol):
    async def create_account(self, ctx: BackendContext) -> CreateAccountResult: ...

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult: ...

    async def reset_password(self, ctx: BackendContext) -> ResetPasswordResult: ...

    async def recharge(
        self, ctx: BackendContext, *, amount_cents: int, bonus_cents: int, total_credit_cents: int
    ) -> RechargeResult: ...

    async def redeem(self, ctx: BackendContext, *, amount_cents: int) -> RedeemResult: ...

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult: ...
