from app.backends.base import BackendError, TransientBackendError
from app.backends.context import BackendContext
from app.backends.ultrapanda.client import UltraPandaClient
from app.backends.ultrapanda.errors import map_code
from app.backends.ultrapanda.passwords import generate_vpower_password
from app.schemas.results import (
    AgentBalanceResult,
    CreateAccountResult,
    ReadBalanceResult,
    RechargeResult,
    RedeemResult,
    ResetPasswordResult,
)


def _cents_to_score(cents: int) -> str:
    """Format integer cents as a 2-decimal-place dollar string for the `score` field."""
    return f"{cents / 100:.2f}"


def _raise_for_code(body: dict, *, op: str, driver: str) -> None:
    """If body['code'] isn't 20000, map it and raise the right BackendError variant."""
    code = body.get("code")
    if code == 20000:
        return
    mapped = map_code(int(code) if isinstance(code, int) else 0, op=op)
    if mapped is None:
        raise TransientBackendError(f"{driver}:malformed_response")
    slug, terminal = mapped
    if terminal:
        raise BackendError(f"{driver}:{slug}")
    raise TransientBackendError(f"{driver}:{slug}")


class UltraPandaBackend:
    """6 ops over the vpower JSON-RPC client. Used for both UltraPanda and VBlink
    (registry alias); driver_prefix on the underlying client distinguishes them."""

    def __init__(self, client: UltraPandaClient) -> None:
        self._client = client

    # ---- AGENT_BALANCE ----

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult:
        token = await self._client.get_or_login()
        body = await self._client.call("/user/CurScore", {"token": token})
        _raise_for_code(body, op="agent_balance", driver=self._client._driver)
        limit = body.get("LimitNum")
        if limit is None:
            raise BackendError(f"{self._client._driver}:agent_balance_missing")
        return AgentBalanceResult(agent_balance_cents=round(float(limit) * 100))

    # ---- READ_BALANCE ----

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        account = self._account_name(ctx)
        body = await self._client.call("/account/getPlayerScore", {"account": account})
        _raise_for_code(body, op="read_balance", driver=self._client._driver)
        cur = body.get("curScore", 0)
        return ReadBalanceResult(balance_cents=round(float(cur) * 100))

    # ---- RESET_PASSWORD ----

    async def reset_password(self, ctx: BackendContext) -> ResetPasswordResult:
        account = self._account_name(ctx)
        pwd = generate_vpower_password()
        body = await self._client.call(
            "/account/updatePlayer",
            {
                "account": account,
                "pwd": pwd,
                "name": "",
                "tel_area_code": "",
                "phone": "",
                "remark": "",
            },
        )
        _raise_for_code(body, op="reset_password", driver=self._client._driver)
        return ResetPasswordResult(password=pwd)

    # ---- RECHARGE ----

    async def recharge(
        self, ctx: BackendContext, *,
        amount_cents: int, bonus_cents: int, total_credit_cents: int,
    ) -> RechargeResult:
        account = self._account_name(ctx)
        body = await self._client.call_throttled(
            "/account/enterScore",
            {
                "account": account,
                "score": _cents_to_score(total_credit_cents),
                "user_type": 0,
            },
            op="recharge",
        )
        _raise_for_code(body, op="recharge", driver=self._client._driver)
        return RechargeResult(balance_cents=None)

    # ---- REDEEM ----

    async def redeem(self, ctx: BackendContext, *, amount_cents: int) -> RedeemResult:
        account = self._account_name(ctx)
        body = await self._client.call_throttled(
            "/account/enterScore",
            {
                "account": account,
                "score": f"-{_cents_to_score(amount_cents)}",
                "user_type": 0,
            },
            op="redeem",
        )
        _raise_for_code(body, op="redeem", driver=self._client._driver)
        return RedeemResult()

    # ---- CREATE_ACCOUNT ----

    async def create_account(self, ctx: BackendContext) -> CreateAccountResult:
        username = ctx.account_username
        if not username:
            raise BackendError(f"{self._client._driver}:account_username_required")
        pwd = generate_vpower_password()
        body = await self._client.call(
            "/account/savePlayer",
            {"account": username, "pwd": pwd},
        )
        _raise_for_code(body, op="create_account", driver=self._client._driver)
        return CreateAccountResult(username=username, password=pwd, external_user_id=None)

    # ---- internal ----

    def _account_name(self, ctx: BackendContext) -> str:
        if ctx.account and ctx.account.username:
            return ctx.account.username
        if ctx.account_username:
            return ctx.account_username
        raise BackendError(f"{self._client._driver}:account_name_required")
