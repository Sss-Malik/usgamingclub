from app.backends.base import BackendError
from app.backends.context import BackendContext
from app.backends.yolo.client import YoloClient
from app.backends.yolo.parsers import parse_agent_score, parse_player_row
from app.backends.yolo.passwords import generate_memorable_password
from app.schemas.results import (
    AgentBalanceResult,
    CreateAccountResult,
    ReadBalanceResult,
    RechargeResult,
    RedeemResult,
    ResetPasswordResult,
)

_RECHARGE_FORM = "App\\Admin\\Actions\\UserRecharge"
_RESET_FORM = "App\\Admin\\Actions\\ResetUserPass"
_PLAYER_LIST = "/admin/player_list"
_DCAT_FORM = "/admin/dcat-api/form"


class YoloBackend:
    def __init__(self, client: YoloClient) -> None:
        self._client = client

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult:
        text = await self._client.get_text("/admin/refresh_score")
        return AgentBalanceResult(agent_balance=parse_agent_score(text))

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        _uid, score = await self._player(ctx)
        return ReadBalanceResult(balance=score)

    async def recharge(self, ctx: BackendContext, *, amount: int) -> RechargeResult:
        await self._user_recharge(ctx, type_=1, amount=amount, step="recharge.post")
        # No balance_after: yolo's post_form success envelope is a Dcat {status,message}
        # dict with no balance field (Appendix A flag) -- nothing honest to retain here.
        return RechargeResult()

    async def redeem(self, ctx: BackendContext, *, amount: int) -> RedeemResult:
        await self._user_recharge(ctx, type_=2, amount=amount, step="redeem.post")
        return RedeemResult()

    async def reset_password(self, ctx: BackendContext) -> ResetPasswordResult:
        uid, account = await self._player_id(ctx)
        pwd = generate_memorable_password()
        await self._client.post_form(_DCAT_FORM, {
            "_form_": _RESET_FORM,
            "UserID": uid, "Accounts": account, "password": pwd,
            "_current_": f"{self._base()}/admin/player_list?",
        }, step="reset.post", phase="primary")
        return ResetPasswordResult(password=pwd)

    async def create_account(self, ctx: BackendContext) -> CreateAccountResult:
        username = ctx.account_username
        if not username:
            raise BackendError("yolo:account_username_required")
        pwd = generate_memorable_password()
        await self._client.post_form(_PLAYER_LIST, {
            "Accounts": username, "NickName": username, "LogonPass": pwd,
            "Recharge_Amount": 0, "RegisterIP": "0.0.0.0",
            # Hidden fields the Dcat create form submits (findings doc §6). The server
            # overwrites/derives most, but several AccountsInfo columns have no DB default
            # (verified in prod: LastLogonIP), so the INSERT fails with SQLSTATE 1364 unless
            # the full browser field set is present. Send them exactly as the UI does.
            "ChannelID": "", "RegAccounts": "", "AgentID": "", "InsurePass": "", "FaceID": "",
            "LastLogonIP": "0.0.0.0", "MemberOrder": "", "MemberExp": "", "RegisterMobile": "",
            "RegisterMachine": "", "BindAgentDate": "", "Nullity": 1,
            "_previous_": f"{self._base()}/admin/player_list",
        }, step="create.post", phase="primary")
        # Follow-up search to resolve the new player's UserID (best-effort; None if not indexed yet).
        # _search() marks external_user_id itself on success (grid column 0).
        external_user_id: str | None = None
        try:
            external_user_id, _score = await self._search(ctx, username)
        except BackendError:
            external_user_id = None
        return CreateAccountResult(username=username, password=pwd, external_user_id=external_user_id)

    # ---- internal ----

    async def _user_recharge(self, ctx: BackendContext, *, type_: int, amount: int, step: str) -> None:
        uid, account = await self._player_id(ctx)
        await self._client.post_form(_DCAT_FORM, {
            "_form_": _RECHARGE_FORM,
            "UserID": uid, "Accounts": account, "type": type_,
            "input_score": str(int(amount)), "Score": "", "remark": "",
            "_current_": f"{self._base()}/admin/player_list?",
        }, step=step, phase="primary")

    async def _player(self, ctx: BackendContext) -> tuple[str, float]:
        """Return (user_id, balance) — searches player_list by account. This IS the
        read-balance op's own HTTP call, so it's tagged `balance.read` (not `resolve.search`)."""
        account = self._account(ctx)
        return await self._search(ctx, account, step="balance.read")

    async def _player_id(self, ctx: BackendContext) -> tuple[str, str]:
        """Return (user_id, account). Prefer cached external_user_id; else search."""
        account = self._account(ctx)
        if ctx.account and ctx.account.external_user_id:
            ctx.diag.mark_external_user_id(ctx.account.external_user_id)
            return ctx.account.external_user_id, account
        uid, _score = await self._search(ctx, account)
        return uid, account

    async def _search(self, ctx: BackendContext, account: str, *,
                      step: str = "resolve.search") -> tuple[str, float]:
        phase = "resolve" if step == "resolve.search" else "primary"
        html = await self._client.get_text(
            _PLAYER_LIST, {"Accounts": account, "_pjax": "#pjax-container"},
            step=step, phase=phase,
        )
        uid, score = parse_player_row(html, account=account)
        ctx.diag.mark_external_user_id(uid)
        return uid, score

    @staticmethod
    def _account(ctx: BackendContext) -> str:
        if ctx.account and ctx.account.username:
            return ctx.account.username
        raise BackendError("yolo:account_required")

    def _base(self) -> str:
        # _current_/_previous_ echo the panel URL; harmless if the host differs in tests.
        return "https://agent.yolo-777.com"
