# app/backends/registry.py
from app.backends.base import BackendError, GameBackend
from app.backends.context import GameCredentials
from app.backends.gamevault.backend import GameVaultBackend
from app.backends.gamevault.client import GameVaultClient
from app.backends.mock.backend import MockBackend
from app.config import Settings


def resolve_backend(
    driver: str | None, *, credentials: GameCredentials, http_client, settings: Settings
) -> GameBackend:
    """Resolve the backend for an operation from its game's backend_driver.

    `null`/`mock` -> MockBackend; `gamevault` -> GameVaultBackend. Unknown -> BackendError.
    """
    key = (driver or "mock").lower()
    if key == "mock":
        return MockBackend(fail=settings.mock_force_fail, fail_reason=settings.mock_force_fail_reason)
    if key == "gamevault":
        return GameVaultBackend(
            GameVaultClient(
                base_url=credentials.api_base_url or "",
                agent_id=credentials.api_agent_id or "",
                secret_key=credentials.api_secret_key or "",
                http_client=http_client,
            )
        )
    raise BackendError(f"unknown_backend_driver:{driver}")
