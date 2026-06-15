from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends.base import BackendError
from app.backends.context import BackendContext
from app.backends.orionstars.backend import OrionStarsBackend
from app.schemas.results import ReadBalanceResult


class MilkyWayBackend(OrionStarsBackend):
    """MilkyWay portal (same 3.0.303 build as OrionStars).

    Only divergence vs. OrionStars: `read_balance` bypasses `getscoreuserid` (which
    re-renders the page without the `credit@totalwin|` prefix on MilkyWay) and instead
    parses the Balance column directly from the ctl16 search result row.
    See findings doc §4.1 portal-difference note.
    """

    def __init__(self, client: AspnetCashierClient) -> None:
        super().__init__(client)

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        # Prefer GameID as the search query when external_user_id is cached (more selective
        # than account name); fall back to the account username when nothing is cached.
        query: str
        if ctx.account and ctx.account.external_user_id and ":" in ctx.account.external_user_id:
            _uid, gid = ctx.account.external_user_id.split(":", 1)
            query = gid
        elif ctx.account and ctx.account.username:
            query = ctx.account.username
        else:
            raise BackendError(f"{self._client._driver}:player_not_found")
        credit = await self._client.milkyway_read_balance(query=query)
        return ReadBalanceResult(balance=float(credit))
