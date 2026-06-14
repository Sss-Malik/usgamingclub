# app/backends/registry.py
from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.session import CookieSessionStore
from app.backends.base import BackendError, GameBackend
from app.backends.context import GameCredentials
from app.backends.gameroom.backend import GameroomBackend
from app.backends.gameroom.client import GameroomClient
from app.backends.gamevault.backend import GameVaultBackend
from app.backends.gamevault.client import GameVaultClient
from app.backends.goldentreasure.backend import GoldenTreasureBackend
from app.backends.goldentreasure.client import GoldenTreasureClient
from app.backends.goldentreasure.session import RedisSessionStore as GTSessionStore
from app.backends.milkyway.backend import MilkyWayBackend
from app.backends.mock.backend import MockBackend
from app.backends.orionstars.backend import OrionStarsBackend
from app.backends.ultrapanda.backend import UltraPandaBackend
from app.backends.ultrapanda.client import UltraPandaClient
from app.backends.ultrapanda.session import RedisTokenStore as VPowerTokenStore
from app.captcha.anticaptcha import AntiCaptchaSolver
from app.config import Settings

# Driver strings that share the GameVault provider's wire protocol (auth, endpoints, envelope).
_GAMEVAULT_PROVIDER_DRIVERS = frozenset({"gamevault", "juwa", "juwa2"})

# Driver strings that share the vpower provider (UltraPanda + VBlink). Same wire protocol,
# only the host differs. Verified byte-identical per the Phase 6 findings doc §10.
_VPOWER_PROVIDER_DRIVERS = frozenset({"ultrapanda", "vblink"})

# Drivers with no server-side idempotency (no order_id/dedupe). The API endpoint passes
# arq _max_tries=1 for these so a worker crash mid-money-op cannot double-apply funds.
NON_IDEMPOTENT_DRIVERS: frozenset[str] = frozenset({
    "gameroom", "goldentreasure", "orionstars", "milkyway", "ultrapanda", "vblink",
})


def resolve_backend(
    driver: str | None, *,
    credentials: GameCredentials,
    http_client,
    settings: Settings,
    session_store=None,                           # Phase 3 — used by gameroom
    redis=None,                                   # Phase 4 — used by goldentreasure (throttle + own session store)
) -> GameBackend:
    """Resolve the backend for an operation from its game's backend_driver.

    `null`/`mock` -> MockBackend.
    `gamevault`/`juwa`/`juwa2` -> GameVaultBackend (same provider, per-game creds).
    `gameroom` -> GameroomBackend (requires session_store).
    `goldentreasure` -> GoldenTreasureBackend (requires redis client; constructs its own SessionStore).
    `orionstars`/`milkyway` -> OrionStarsBackend / MilkyWayBackend over the shared ASP.NET
        cashier client (requires redis client + settings.anticaptcha_api_key for captcha-aware login).
    `ultrapanda`/`vblink` -> UltraPandaBackend over the shared vpower client (requires redis;
        VBlink is a registry alias — same class, driver_prefix distinguishes them in logs/keys).
    Unknown -> BackendError.
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
    if key == "goldentreasure":
        if not (credentials.backend_url and credentials.backend_username and credentials.backend_password):
            raise BackendError("missing_goldentreasure_credentials")
        if redis is None:
            raise BackendError("missing_redis_client")
        return GoldenTreasureBackend(
            GoldenTreasureClient(
                base_url=credentials.backend_url,
                username=credentials.backend_username,
                password=credentials.backend_password,
                http_client=http_client,
                session_store=GTSessionStore(redis),
                redis=redis,
                game_id=credentials.game_id,
            )
        )
    if key in {"orionstars", "milkyway"}:
        if not (credentials.backend_url and credentials.backend_username and credentials.backend_password):
            raise BackendError(f"missing_{key}_credentials")
        if redis is None:
            raise BackendError("missing_redis_client")
        if not settings.anticaptcha_api_key:
            raise BackendError("missing_anticaptcha_api_key")
        client = AspnetCashierClient(
            base_url=credentials.backend_url,
            username=credentials.backend_username,
            password=credentials.backend_password,
            http_client=http_client,
            session_store=CookieSessionStore(redis),
            captcha_solver=AntiCaptchaSolver(api_key=settings.anticaptcha_api_key),
            game_id=credentials.game_id,
            session_ttl_seconds=settings.aspnet_session_ttl_seconds,
            lock_ttl_seconds=settings.aspnet_lock_ttl_seconds,
            lock_acquire_timeout_seconds=settings.aspnet_lock_acquire_timeout_seconds,
            captcha_login_max_attempts=settings.captcha_login_max_attempts,
            driver_prefix=key,
        )
        return OrionStarsBackend(client) if key == "orionstars" else MilkyWayBackend(client)
    if key in _VPOWER_PROVIDER_DRIVERS:
        if not (credentials.backend_url and credentials.backend_username and credentials.backend_password):
            raise BackendError(f"missing_{key}_credentials")
        if redis is None:
            raise BackendError("missing_redis_client")
        return UltraPandaBackend(
            UltraPandaClient(
                base_url=credentials.backend_url,
                username=credentials.backend_username,
                password=credentials.backend_password,
                http_client=http_client,
                session_store=VPowerTokenStore(redis),
                redis=redis,
                game_id=credentials.game_id,
                session_ttl_seconds=settings.vpower_session_ttl_seconds,
                throttle_ttl_seconds=settings.vpower_throttle_ttl_seconds,
                throttle_acquire_timeout_seconds=settings.vpower_throttle_acquire_timeout_seconds,
                session_lock_ttl_seconds=settings.vpower_session_lock_ttl_seconds,
                session_lock_acquire_timeout_seconds=settings.vpower_session_lock_acquire_timeout_seconds,
                driver_prefix=key,
            )
        )
    raise BackendError(f"unknown_backend_driver:{driver}")
