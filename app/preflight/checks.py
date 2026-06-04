from sqlalchemy.ext.asyncio import AsyncSession

from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.db.repositories import GameAccountsRepository, GamesRepository

ACCOUNT_SCOPED_TYPES = {"READ_BALANCE", "RESET_PASSWORD", "RECHARGE", "REDEEM"}


class PreflightError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


async def build_context(
    session: AsyncSession,
    *,
    type: str,
    game_id: int,
    game_account_id: int | None,
    user_id: int | None,
    idempotency_key: str = "",
    account_username: str | None = None,
) -> BackendContext:
    game = await GamesRepository(session).get(game_id)
    if game is None:
        raise PreflightError(f"game_not_found: {game_id}")

    credentials = GameCredentials(
        game_id=game.id,
        name=game.name,
        backend_url=game.backend_url,
        login_page_url=game.login_page_url,
        backend_username=game.backend_username,
        backend_password=game.backend_password,
        api_base_url=game.api_base_url,
        api_agent_id=game.api_agent_id,
        api_secret_key=game.api_secret_key,
        binding_key=game.binding_key,
        backend_driver=game.backend_driver,
    )

    if (game.backend_driver or "").lower() == "gamevault" and not (
        game.api_base_url and game.api_agent_id and game.api_secret_key
    ):
        raise PreflightError("missing_gamevault_credentials")

    account: AccountIdentity | None = None
    if type in ACCOUNT_SCOPED_TYPES:
        if game_account_id is None:
            raise PreflightError("missing_game_account_id")
        acct = await GameAccountsRepository(session).get(game_account_id)
        if acct is None:
            raise PreflightError(f"game_account_not_found: {game_account_id}")
        account = AccountIdentity(
            game_account_id=acct.id,
            user_id=acct.user_id,
            game_id=acct.game_id,
            username=acct.username,
            external_user_id=acct.external_user_id,
        )

    return BackendContext(
        credentials=credentials,
        user_id=user_id,
        account=account,
        idempotency_key=idempotency_key,
        account_username=account_username,
    )
