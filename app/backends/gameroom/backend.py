# app/backends/gameroom/backend.py
import math

from app.backends.base import BackendError
from app.backends.context import BackendContext
from app.backends.gameroom.client import GameroomClient
from app.backends.gameroom.passwords import (
    generate_memorable_complex_password,
    generate_memorable_password,
)
from app.schemas.results import (
    AgentBalanceResult,
    CreateAccountResult,
    ReadBalanceResult,
    RechargeResult,
    RedeemResult,
    ResetPasswordResult,
)


def _to_cents(value) -> int:
    return round(float(value) * 100)


def _to_cents_opt(value) -> int | None:
    return None if value is None else _to_cents(value)


def _to_dollars(cents: int) -> str:
    return str(math.ceil(cents / 100))


class GameroomBackend:
    def __init__(self, client: GameroomClient) -> None:
        self._client = client

    # ---- AGENT_BALANCE ----

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult:
        # /api/agent/getMoney response shape isn't pinned in the findings doc; .call() unwraps
        # `data` if it's a dict, else returns the top-level keys (where login's `money` lives).
        data = await self._client.call("POST", "/api/agent/getMoney")
        value = data.get("money")
        if value is None:
            raise BackendError("gameroom:agent_balance_missing")
        return AgentBalanceResult(agent_balance_cents=_to_cents(value))

    # ---- READ_BALANCE ----

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        pid = await self._player_id(ctx)
        data = await self._client.call("GET", "/api/player/agentMoney", params={"id": pid})
        return ReadBalanceResult(balance_cents=_to_cents(data.get("balance", 0)))

    # ---- RESET_PASSWORD ----

    async def reset_password(self, ctx: BackendContext) -> ResetPasswordResult:
        pid = await self._player_id(ctx)
        pwd = generate_memorable_complex_password()
        await self._client.call(
            "POST", "/api/player/reset",
            fields={"id": pid, "password": pwd, "password_confirmation": pwd},
        )
        return ResetPasswordResult(password=pwd)

    # ---- RECHARGE ----

    async def recharge(
        self, ctx: BackendContext, *,
        amount_cents: int, bonus_cents: int, total_credit_cents: int,
    ) -> RechargeResult:
        pid = await self._player_id(ctx)
        # available_balance: server ignores the value but the field is required (empty OK).
        # bonus=0: we already credit `total_credit_cents` via balance; bonus is on top per the doc.
        # remark="": UUIDs have hyphens which fail [A-Za-z0-9]; empty is allowed.
        data = await self._client.call(
            "POST", "/api/player/agentRecharge",
            fields={
                "id": pid,
                "available_balance": "",
                "opera_type": 0,
                "bonus": 0,
                "balance": _to_dollars(total_credit_cents),
                "remark": "",
            },
        )
        return RechargeResult(balance_cents=_to_cents_opt(data.get("total_balance")))

    # ---- REDEEM ----

    async def redeem(self, ctx: BackendContext, *, amount_cents: int) -> RedeemResult:
        pid = await self._player_id(ctx)
        # agentWithdraw success returns no `data` block; treat as success and omit balance_cents.
        await self._client.call(
            "POST", "/api/player/agentWithdraw",
            fields={
                "id": pid,
                "customer_balance": "",
                "opera_type": 1,
                "balance": _to_dollars(amount_cents),
                "remark": "",
            },
        )
        return RedeemResult()

    # ---- CREATE_ACCOUNT ----

    async def create_account(self, ctx: BackendContext) -> CreateAccountResult:
        if not ctx.account_username:
            raise BackendError("account_username_required")
        pwd = generate_memorable_password()  # alphanumeric 6-12 (satisfies the create rule)
        data = await self._client.call(
            "POST", "/api/player/playerInsert",
            fields={
                "username": ctx.account_username,
                "nickname": ctx.account_username,
                "money": 0,                   # send defensively; missing money triggers a server bug
                "password": pwd,
                "password_confirmation": pwd,
            },
        )
        new_id = data.get("id")
        if new_id is None:
            raise BackendError("gameroom:playerInsert_missing_id")
        return CreateAccountResult(
            username=ctx.account_username,
            password=pwd,
            external_user_id=str(new_id),
        )

    # ---- internal: player_id resolution ----

    async def _player_id(self, ctx: BackendContext) -> str:
        """Prefer cached external_user_id; else exact-match the player via userList."""
        if ctx.account and ctx.account.external_user_id:
            return ctx.account.external_user_id
        if ctx.account and ctx.account.username:
            envelope = await self._client.call_raw(
                "GET", "/api/player/userList",
                params={"page": 1, "limit": 20, "account": ctx.account.username},
            )
            rows = envelope.get("data") or []
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and row.get("Account") == ctx.account.username:
                        rid = row.get("id") or row.get("Id")
                        if rid is not None:
                            return str(rid)
            raise BackendError("gameroom:player_not_found")
        raise BackendError("gameroom:player_not_found")
