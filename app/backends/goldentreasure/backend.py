# app/backends/goldentreasure/backend.py
from app.backends.base import BackendError
from app.backends.context import BackendContext
from app.backends.goldentreasure.client import GoldenTreasureClient
from app.backends.goldentreasure.passwords import generate_memorable_password
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


class GoldenTreasureBackend:
    def __init__(self, client: GoldenTreasureClient) -> None:
        self._client = client

    # ---- AGENT_BALANCE ----

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult:
        data = await self._client.call("/api/user/CurScore", {})
        v = data.get("LimitNum")
        if v is None:
            raise BackendError("gtreasure:agent_balance_missing")
        return AgentBalanceResult(agent_balance=_balance(v))

    # ---- READ_BALANCE ----

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        username = _require_username(ctx)
        data = await self._client.call(
            "/api/account/getPlayerScore", {"account": username},
        )
        return ReadBalanceResult(balance=_balance(data.get("curScore", 0)))

    # ---- CREATE_ACCOUNT ----

    async def create_account(self, ctx: BackendContext) -> CreateAccountResult:
        if not ctx.account_username:
            raise BackendError("account_username_required")
        pwd = generate_memorable_password()       # alphanumeric (satisfies 6-16 letters+digits rule)
        await self._client.call(
            "/api/account/savePlayer",
            {
                "account": ctx.account_username,
                "pwd": pwd,
                "score": "0",
                "name": "", "phone": "", "tel_area_code": "", "remark": "",
            },
            throttle=True,
        )
        # savePlayer doesn't return a uid (spec GT4) -> external_user_id=None.
        return CreateAccountResult(
            username=ctx.account_username, password=pwd, external_user_id=None,
        )

    # ---- RESET_PASSWORD ----

    async def reset_password(self, ctx: BackendContext) -> ResetPasswordResult:
        username = _require_username(ctx)
        pwd = generate_memorable_password()
        await self._client.call(
            "/api/account/updatePlayer",
            {
                "account": username,
                "pwd": pwd,
                "name": "", "phone": "", "remark": "", "tel_area_code": "",
            },
            # NOT throttled (spec GT7).
        )
        return ResetPasswordResult(password=pwd)

    # ---- RECHARGE ----

    async def recharge(self, ctx: BackendContext, *, amount: int) -> RechargeResult:
        username = _require_username(ctx)
        await self._client.call(
            "/api/account/enterScore",
            {
                "account": username,
                "score": str(int(amount)),
                "remark": "",
                "user_type": "player",
            },
            throttle=True,
        )
        # enterScore success has no balance; we omit it (contract makes it optional).
        return RechargeResult()

    # ---- REDEEM ----

    async def redeem(self, ctx: BackendContext, *, amount: int) -> RedeemResult:
        username = _require_username(ctx)
        await self._client.call(
            "/api/account/enterScore",
            {
                "account": username,
                "score": str(-int(amount)),               # negative score = withdraw
                "remark": "",
                "user_type": "player",
            },
            throttle=True,
        )
        return RedeemResult()


def _require_username(ctx: BackendContext) -> str:
    """Defensive: preflight populates `ctx.account` for account-scoped ops, but if it didn't
    (e.g. an op routed here directly bypassing preflight), surface a clean error rather than
    crashing with AttributeError."""
    if ctx.account is None or not ctx.account.username:
        raise BackendError("gtreasure:account_required")
    return ctx.account.username
