# app/backends/gameroom/backend.py
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


def _balance(value) -> float:
    return float(value)


def _balance_opt(value) -> float | None:
    return None if value is None else float(value)


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
        return AgentBalanceResult(agent_balance=_balance(value))

    # ---- READ_BALANCE ----

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        pid = await self._player_id(ctx)
        data = await self._client.call("GET", "/api/player/agentMoney", params={"id": pid})
        return ReadBalanceResult(balance=_balance(data.get("balance", 0)))

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

    async def recharge(self, ctx: BackendContext, *, amount: int) -> RechargeResult:
        pid = await self._player_id(ctx)
        # Pre-fetch the current agent balance: the server rejects a stale or empty
        # `available_balance` with "Available balance has changed. Please refresh and recharge again."
        # (Verified in production; the findings doc's "value can be stale" note was wrong.)
        snapshot = await self._agent_money(pid)
        # bonus=0: we already credit `amount` via balance; bonus is on top per the doc.
        # remark="": UUIDs have hyphens which fail [A-Za-z0-9]; empty is allowed.
        data = await self._client.call(
            "POST", "/api/player/agentRecharge",
            fields={
                "id": pid,
                "available_balance": str(snapshot.get("cusBlance", "")),
                "opera_type": 0,
                "bonus": 0,
                "balance": str(int(amount)),
                "remark": "",
            },
        )
        return RechargeResult(balance=_balance_opt(data.get("total_balance")))

    # ---- REDEEM ----

    async def redeem(self, ctx: BackendContext, *, amount: int) -> RedeemResult:
        pid = await self._player_id(ctx)
        # Pre-fetch the player balance: same staleness validation as recharge applies to
        # `customer_balance`. agentMoney returns both fields in one call.
        snapshot = await self._agent_money(pid)
        # agentWithdraw success returns no `data` block; treat as success and omit balance.
        await self._client.call(
            "POST", "/api/player/agentWithdraw",
            fields={
                "id": pid,
                "customer_balance": str(snapshot.get("balance", "")),
                "opera_type": 1,
                "balance": str(int(amount)),
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

    # ---- internal: helpers ----

    async def _agent_money(self, player_id: str) -> dict:
        """Fetch the current player+agent balance pair. Used to populate the snapshot fields
        (`available_balance` for recharge, `customer_balance` for withdraw) — the server validates
        these against its current ledger and rejects mismatches.
        Returns the `data` dict: {"username": ..., "balance": <int>, "cusBlance": <str>}.
        """
        data = await self._client.call(
            "GET", "/api/player/agentMoney", params={"id": player_id},
        )
        return data if isinstance(data, dict) else {}

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
