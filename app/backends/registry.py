# app/backends/registry.py
from app.backends.base import BackendError, GameBackend
from app.backends.context import GameCredentials
from app.backends.gameroom.backend import GameroomBackend
from app.backends.gameroom.client import GameroomClient
from app.backends.gamevault.backend import GameVaultBackend
from app.backends.gamevault.client import GameVaultClient
from app.backends.mock.backend import MockBackend
from app.config import Settings

# Driver strings that share the GameVault provider's wire protocol (auth, endpoints, envelope).
_GAMEVAULT_PROVIDER_DRIVERS = frozenset({"gamevault", "juwa", "juwa2"})

# Drivers with no server-side idempotency (no order_id/dedupe). The API endpoint passes
# arq _max_tries=1 for these so a worker crash mid-money-op cannot double-apply funds.
NON_IDEMPOTENT_DRIVERS: frozenset[str] = frozenset({"gameroom"})


def resolve_backend(
    driver: str | None, *,
    credentials: GameCredentials,
    http_client,
    settings: Settings,
    session_store=None,
) -> GameBackend:
    """Resolve the backend for an operation from its game's backend_driver.

    `null`/`mock` -> MockBackend; `gamevault`/`juwa`/`juwa2` -> GameVaultBackend (same provider,
    per-game creds); `gameroom` -> GameroomBackend (requires session_store). Unknown -> BackendError.
    """
    key = (driver or "mock").lower()
    if key == "mock":
        return MockBackend(fail=settings.mock_force_fail, fail_reason=settings.mock_force_fail_reason)
    if key in _GAMEVAULT_PROVIDER_DRIVERS:
        if not (credentials.api_base_url and credentials.api_agent_id and credentials.api_secret_key):
            raise BackendError("missing_gamevault_credentials")
        return GameVaultBackend(
            GameVaultClient(
                base_url=credentials.api_base_url,
                agent_id=credentials.api_agent_id,
                secret_key=credentials.api_secret_key,
                http_client=http_client,
            )
        )
    if key == "gameroom":
        if not (credentials.backend_url and credentials.backend_username and credentials.backend_password):
            raise BackendError("missing_gameroom_credentials")
        if session_store is None:
            raise BackendError("missing_session_store")
        return GameroomBackend(
            GameroomClient(
                base_url=credentials.backend_url,
                username=credentials.backend_username,
                password=credentials.backend_password,
                http_client=http_client,
                session_store=session_store,
                game_id=credentials.game_id,
            )
        )
    raise BackendError(f"unknown_backend_driver:{driver}")
