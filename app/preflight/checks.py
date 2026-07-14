from sqlalchemy.ext.asyncio import AsyncSession

from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.db.repositories import GameAccountsRepository, GamesRepository

ACCOUNT_SCOPED_TYPES = {"READ_BALANCE", "RESET_PASSWORD", "RECHARGE", "REDEEM", "FREEPLAY"}

_GAMEVAULT_DRIVERS = {"gamevault", "juwa", "juwa2"}


class PreflightError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


async def build_context(
    session: AsyncSession,
    *,
    type: str,
    backend_name: str,
    username: str | None,
    user_id: int | None,
    idempotency_key: str = "",
    account_username: str | None = None,
    diagnostics=None,
    op_id: str | None = None,
    attempt: int = 1,
) -> BackendContext:
    game = await GamesRepository(session).get_by_name(backend_name)
    if game is None:
        raise PreflightError(f"game_not_found: {backend_name}")

    credentials = GameCredentials(
        game_id=game.id,
        name=game.name,
        backend_url=game.backend_url,
        login_page_url=game.login_url,
        backend_username=game.username,
        backend_password=game.password,
        api_base_url=game.api_base_url,
        api_agent_id=game.api_agent_id,
        api_secret_key=game.api_secret_key,
        binding_key=game.binding_key,
        backend_driver=game.backend_driver,
    )

    driver = (game.backend_driver or "").lower()
    if driver in _GAMEVAULT_DRIVERS and not (
        game.api_base_url and game.api_agent_id and game.api_secret_key
    ):
        raise PreflightError(f"missing_{driver}_credentials")
    if driver in {"gameroom", "goldentreasure", "milkyway", "firekirin", "pandamaster",
                  "orionstars", "ultrapanda", "vblink", "yolo"} and not (
        game.backend_url and game.username and game.password
    ):
        raise PreflightError(f"missing_{driver}_credentials")

    account: AccountIdentity | None = None
    if type in ACCOUNT_SCOPED_TYPES:
        if not username:
            raise PreflightError("missing_username")
        acct = await GameAccountsRepository(session).get_by_username(game.id, username)
        if acct is None:
            raise PreflightError(f"game_account_not_found: {username}")
        account = AccountIdentity(
            game_account_id=acct.id,
            user_id=acct.user_id,
            game_id=acct.game_id,
            username=acct.username,
            external_user_id=acct.id_from_backend,
        )

    return BackendContext(
        credentials=credentials,
        user_id=user_id,
        account=account,
        idempotency_key=idempotency_key,
        account_username=account_username,
        diagnostics=diagnostics,
        op_id=op_id,
        attempt=attempt,
    )
