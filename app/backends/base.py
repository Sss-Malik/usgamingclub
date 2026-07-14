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
    """Raised when a game backend call fails in a way that should be reported as status:failed.

    `reason` is the player-facing slug (unchanged). The optional `provider_*` fields carry
    structured provider detail for the webhook `diagnostics` channel; they are never used for
    the player-facing message.
    """

    def __init__(
        self,
        reason: str,
        *,
        provider_http_status: int | None = None,
        provider_code: str | int | None = None,
        provider_message: str | None = None,
    ) -> None:
        self.reason = reason
        self.provider_http_status = provider_http_status
        self.provider_code = provider_code
        self.provider_message = provider_message
        super().__init__(reason)


class TransientBackendError(BackendError):
    """A backend failure that is safe to retry (timeout, 5xx, transient business code).

    The executor does NOT cache these, so an arq re-run will retry the backend call.
    """


class GameBackend(Protocol):
    async def create_account(self, ctx: BackendContext) -> CreateAccountResult: ...

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult: ...

    async def reset_password(self, ctx: BackendContext) -> ResetPasswordResult: ...

    async def recharge(self, ctx: BackendContext, *, amount: int) -> RechargeResult: ...

    async def redeem(self, ctx: BackendContext, *, amount: int) -> RedeemResult: ...

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult: ...
