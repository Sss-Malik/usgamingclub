# app/backends/mock/backend.py
from app.backends.base import BackendError
from app.backends.context import BackendContext
from app.schemas.results import (
    AgentBalanceResult,
    CreateAccountResult,
    ReadBalanceResult,
    RechargeResult,
    RedeemResult,
    ResetPasswordResult,
)


class MockBackend:
    """Deterministic, contract-valid backend used to prove the control plane (Phase 1)."""

    def __init__(self, *, fail: bool = False, fail_reason: str = "forced mock failure") -> None:
        self._fail = fail
        self._fail_reason = fail_reason

    def _maybe_fail(self) -> None:
        if self._fail:
            raise BackendError(self._fail_reason)

    async def create_account(self, ctx: BackendContext) -> CreateAccountResult:
        self._maybe_fail()
        username = ctx.account_username or f"mock_{ctx.user_id}_{ctx.credentials.game_id}"
        return CreateAccountResult(
            username=username,
            password="MockPass123!",
            external_user_id=f"EXT{ctx.user_id}{ctx.credentials.game_id}",
        )

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        self._maybe_fail()
        return ReadBalanceResult(balance_cents=12750)

    async def reset_password(self, ctx: BackendContext) -> ResetPasswordResult:
        self._maybe_fail()
        return ResetPasswordResult(password="MockReset123!")

    async def recharge(
        self, ctx: BackendContext, *, amount_cents: int, bonus_cents: int, total_credit_cents: int
    ) -> RechargeResult:
        self._maybe_fail()
        return RechargeResult(balance_cents=total_credit_cents)

    async def redeem(self, ctx: BackendContext, *, amount_cents: int) -> RedeemResult:
        self._maybe_fail()
        return RedeemResult(balance_cents=0)

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult:
        self._maybe_fail()
        return AgentBalanceResult(agent_balance_cents=100_000)
