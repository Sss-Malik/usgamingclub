import math
from datetime import datetime

from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.passwords import generate_aspnet_password
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


def _to_cents(value: str | float) -> int:
    return round(float(value) * 100)


def _to_dollars(cents: int) -> str:
    return str(math.ceil(cents / 100))


def _now_query_param() -> str:
    # CreateAccount.aspx?time=<dd/MM/yyyy HH:mm:ss> (URL-encoded by httpx).
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


class OrionStarsBackend:
    """OrionStars cashier backend. Reads balance via the getscoreuserid POST."""

    def __init__(self, client: AspnetCashierClient) -> None:
        self._client = client

    # ---- AGENT_BALANCE ----

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult:
        dollars = await self._client.fetch_agent_balance_dollars()
        return AgentBalanceResult(agent_balance_cents=dollars * 100)

    # ---- READ_BALANCE ----

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        uid, _gid = await self._player_ids(ctx)
        credit, _totalwin = await self._client.post_getscoreuserid(uid)
        return ReadBalanceResult(balance_cents=_to_cents(credit))

    # ---- RESET_PASSWORD ----

    async def reset_password(self, ctx: BackendContext) -> ResetPasswordResult:
        uid, gid = await self._player_ids(ctx)
        dialog_url, _ = await self._client.get_dialog_url(tourl=2, uid=uid, gid=gid)
        pwd = generate_aspnet_password()
        text = await self._client.submit_dialog(
            dialog_url=dialog_url,
            extra_fields={"txtConfirmPass": pwd, "txtSureConfirmPass": pwd},
        )
        kind, args = self._client.classify(text)
        if kind == "success":
            return ResetPasswordResult(password=pwd)
        raise self._client.business_failure_to_error(args[0] if args else "")

    # ---- RECHARGE ----

    async def recharge(
        self, ctx: BackendContext, *,
        amount_cents: int, bonus_cents: int, total_credit_cents: int,
    ) -> RechargeResult:
        uid, gid = await self._player_ids(ctx)
        dialog_url, _ = await self._client.get_dialog_url(tourl=0, uid=uid, gid=gid)
        text = await self._client.submit_dialog(
            dialog_url=dialog_url,
            extra_fields={"txtAddGold": _to_dollars(amount_cents), "txtReason": ""},
        )
        kind, args = self._client.classify(text)
        if kind == "success":
            return RechargeResult(balance_cents=None)   # player balance not in this response
        raise self._client.business_failure_to_error(args[0] if args else "")

    # ---- REDEEM ----

    async def redeem(self, ctx: BackendContext, *, amount_cents: int) -> RedeemResult:
        uid, gid = await self._player_ids(ctx)
        dialog_url, _ = await self._client.get_dialog_url(tourl=1, uid=uid, gid=gid)
        text = await self._client.submit_dialog(
            dialog_url=dialog_url,
            extra_fields={"txtAddGold": _to_dollars(amount_cents), "txtReason": ""},
        )
        kind, args = self._client.classify(text)
        if kind == "success":
            return RedeemResult()
        raise self._client.business_failure_to_error(args[0] if args else "")

    # ---- CREATE_ACCOUNT ----

    async def create_account(self, ctx: BackendContext) -> CreateAccountResult:
        username = ctx.account_username
        if not username:
            raise BackendError(f"{self._client._driver}:account_username_required")
        pwd = generate_aspnet_password()
        time_q = _now_query_param()
        # GET the form to scrape viewstate (CreateAccount has EnableEventValidation=true).
        get_body = await self._client.request_text(
            "GET", "/Module/AccountManager/CreateAccount.aspx", params={"time": time_q},
        )
        from app.backends._aspnet_cashier.parsers import parse_viewstate
        vs = parse_viewstate(get_body)
        form = {
            "__EVENTTARGET": "ctl07",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": vs.viewstate,
            "__VIEWSTATEGENERATOR": vs.viewstate_generator,
            "__EVENTVALIDATION": vs.event_validation or "",
            "txtAccount": username,
            "txtNickName": username,
            "txtLogonPass": pwd,
            "txtLogonPass2": pwd,
        }
        text = await self._client.request_text(
            "POST", "/Module/AccountManager/CreateAccount.aspx",
            params={"time": time_q}, form=form,
        )
        kind, args = self._client.classify(text)
        if kind != "success":
            raise self._client.business_failure_to_error(args[0] if args else "")
        # Follow-up search to obtain UID:GID for the new account.
        pairs = await self._client.search_account(username)
        if not pairs:
            raise BackendError(f"{self._client._driver}:create_followup_search_no_rows")
        uid, gid = pairs[0]
        return CreateAccountResult(
            username=username, password=pwd, external_user_id=f"{uid}:{gid}",
        )

    # ---- internal ----

    async def _player_ids(self, ctx: BackendContext) -> tuple[str, str]:
        """Return (UserID, GameID) for ctx.account: split cached external_user_id or search."""
        if ctx.account and ctx.account.external_user_id and ":" in ctx.account.external_user_id:
            uid, gid = ctx.account.external_user_id.split(":", 1)
            return uid, gid
        if ctx.account and ctx.account.username:
            pairs = await self._client.search_account(ctx.account.username)
            if pairs:
                return pairs[0]
            raise BackendError(f"{self._client._driver}:player_not_found")
        raise BackendError(f"{self._client._driver}:player_not_found")
