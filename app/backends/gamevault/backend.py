# app/backends/gamevault/backend.py
import math

from app.backends.base import BackendError
from app.backends.context import BackendContext
from app.backends.gamevault.client import GameVaultClient
from app.backends.gamevault.passwords import generate_memorable_password
from app.schemas.results import (
    AgentBalanceResult,
    CreateAccountResult,
    ReadBalanceResult,
    RechargeResult,
    RedeemResult,
    ResetPasswordResult,
)


def _to_cents(value: str | int | float) -> int:
    return round(float(value) * 100)


def _to_cents_opt(value: str | int | float | None) -> int | None:
    return None if value is None else _to_cents(value)


def _to_dollars(cents: int) -> str:
    return str(math.ceil(cents / 100))


class GameVaultBackend:
    def __init__(self, client: GameVaultClient) -> None:
        self._client = client

    async def _user_id(self, ctx: BackendContext) -> str:
        if ctx.account and ctx.account.external_user_id:
            return ctx.account.external_user_id
        if ctx.account and ctx.account.username:
            data = await self._client.call(
                "/api/external/getUserID", {"account_name": ctx.account.username}
            )
            return str(data["user_id"])
        raise BackendError("user_id_unresolved")

    async def create_account(self, ctx: BackendContext) -> CreateAccountResult:
        if not ctx.account_username:
            raise BackendError("account_username_required")
        pwd = generate_memorable_password()
        data = await self._client.call(
            "/api/external/addUser", {"account": ctx.account_username, "login_pwd": pwd}
        )
        return CreateAccountResult(
            username=ctx.account_username, password=pwd, external_user_id=str(data["user_id"])
        )

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        uid = await self._user_id(ctx)
        data = await self._client.call("/api/external/userBalance", {"user_id": uid})
        return ReadBalanceResult(balance_cents=_to_cents(data["user_balance"]))

    async def reset_password(self, ctx: BackendContext) -> ResetPasswordResult:
        uid = await self._user_id(ctx)
        pwd = generate_memorable_password()
        await self._client.call("/api/external/resetPassword", {"user_id": uid, "login_pwd": pwd})
        return ResetPasswordResult(password=pwd)

    async def recharge(
        self, ctx: BackendContext, *, amount_cents: int, bonus_cents: int, total_credit_cents: int
    ) -> RechargeResult:
        uid = await self._user_id(ctx)
        data = await self._client.call(
            "/api/external/recharge",
            {"user_id": uid, "amount": _to_dollars(total_credit_cents), "order_id": ctx.idempotency_key},
        )
        return RechargeResult(balance_cents=_to_cents_opt(data.get("user_balance")))

    async def redeem(self, ctx: BackendContext, *, amount_cents: int) -> RedeemResult:
        uid = await self._user_id(ctx)
        data = await self._client.call(
            "/api/external/withdraw",
            {"user_id": uid, "amount": _to_dollars(amount_cents), "order_id": ctx.idempotency_key},
        )
        return RedeemResult(balance_cents=_to_cents_opt(data.get("user_balance")))

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult:
        data = await self._client.call("/api/external/agentBalance", {})
        return AgentBalanceResult(agent_balance_cents=_to_cents(data["agent_balance"]))
