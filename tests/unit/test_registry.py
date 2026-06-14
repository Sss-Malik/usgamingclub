# tests/unit/test_registry.py
import pytest

from app.backends.base import BackendError
from app.backends.context import GameCredentials
from app.backends.gameroom.backend import GameroomBackend
from app.backends.gameroom.session import InMemorySessionStore
from app.backends.gamevault.backend import GameVaultBackend
from app.backends.goldentreasure.backend import GoldenTreasureBackend
from app.backends.mock.backend import MockBackend
from app.backends.registry import NON_IDEMPOTENT_DRIVERS, resolve_backend
from app.config import Settings


def _creds(driver):
    return GameCredentials(
        game_id=9, name="g", backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url="https://gv.test", api_agent_id="11", api_secret_key="s",
        binding_key=None, backend_driver=driver,
    )


def _settings():
    return Settings(python_signing_secret="s")


def test_none_or_mock_returns_mock_backend():
    s = _settings()
    assert isinstance(resolve_backend(None, credentials=_creds(None), http_client=None, settings=s), MockBackend)
    assert isinstance(resolve_backend("mock", credentials=_creds("mock"), http_client=None, settings=s), MockBackend)


def test_gamevault_driver_returns_gamevault_backend():
    s = _settings()
    backend = resolve_backend("gamevault", credentials=_creds("gamevault"), http_client=object(), settings=s)
    assert isinstance(backend, GameVaultBackend)


def test_unknown_driver_raises():
    s = _settings()
    with pytest.raises(BackendError):
        resolve_backend("nope", credentials=_creds("nope"), http_client=None, settings=s)


def test_gamevault_missing_credentials_raises():
    s = _settings()
    creds = GameCredentials(
        game_id=9, name="g", backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="gamevault",
    )
    with pytest.raises(BackendError) as ei:
        resolve_backend("gamevault", credentials=creds, http_client=object(), settings=s)
    assert ei.value.reason == "missing_gamevault_credentials"


def test_juwa_driver_routes_to_gamevault_backend():
    # Juwa is the same provider as GameVault; per-game creds distinguish them. Both driver
    # strings resolve to the same backend class.
    s = _settings()
    backend = resolve_backend("juwa", credentials=_creds("juwa"), http_client=object(), settings=s)
    assert isinstance(backend, GameVaultBackend)


def test_juwa_missing_credentials_raises_same_reason():
    # Same missing-creds guard as gamevault; same reason slug so logs/dashboards group by provider.
    s = _settings()
    creds = GameCredentials(
        game_id=9, name="g", backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="juwa",
    )
    with pytest.raises(BackendError) as ei:
        resolve_backend("juwa", credentials=creds, http_client=object(), settings=s)
    assert ei.value.reason == "missing_gamevault_credentials"


def test_juwa2_driver_routes_to_gamevault_backend():
    # juwa2 is another sibling game on the same provider as gamevault/juwa.
    s = _settings()
    backend = resolve_backend("juwa2", credentials=_creds("juwa2"), http_client=object(), settings=s)
    assert isinstance(backend, GameVaultBackend)


def test_juwa2_missing_credentials_raises_same_reason():
    s = _settings()
    creds = GameCredentials(
        game_id=9, name="g", backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="juwa2",
    )
    with pytest.raises(BackendError) as ei:
        resolve_backend("juwa2", credentials=creds, http_client=object(), settings=s)
    assert ei.value.reason == "missing_gamevault_credentials"


def _gameroom_creds():
    return GameCredentials(
        game_id=11, name="g",
        backend_url="https://gr.test", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="gameroom",
    )


def test_non_idempotent_drivers_contains_gameroom():
    assert "gameroom" in NON_IDEMPOTENT_DRIVERS
    # gamevault family is deliberately NOT in this set (order_id dedupe makes retries safe)
    assert {"gamevault", "juwa", "juwa2"}.isdisjoint(NON_IDEMPOTENT_DRIVERS)


def test_gameroom_driver_routes_to_gameroom_backend():
    s = _settings()
    backend = resolve_backend(
        "gameroom", credentials=_gameroom_creds(),
        http_client=object(), settings=s, session_store=InMemorySessionStore(),
    )
    assert isinstance(backend, GameroomBackend)


def test_gameroom_missing_session_store_raises():
    s = _settings()
    with pytest.raises(BackendError) as ei:
        resolve_backend(
            "gameroom", credentials=_gameroom_creds(),
            http_client=object(), settings=s, session_store=None,
        )
    assert ei.value.reason == "missing_session_store"


def test_gameroom_missing_credentials_raises():
    s = _settings()
    creds = GameCredentials(
        game_id=11, name="g",
        backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="gameroom",
    )
    with pytest.raises(BackendError) as ei:
        resolve_backend(
            "gameroom", credentials=creds,
            http_client=object(), settings=s, session_store=InMemorySessionStore(),
        )
    assert ei.value.reason == "missing_gameroom_credentials"


def _gt_creds():
    return GameCredentials(
        game_id=13, name="g",
        backend_url="https://gt.test", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="goldentreasure",
    )


def test_non_idempotent_drivers_contains_goldentreasure():
    assert "goldentreasure" in NON_IDEMPOTENT_DRIVERS
    assert "gameroom" in NON_IDEMPOTENT_DRIVERS              # Phase 3
    # gamevault family is deliberately NOT in this set (order_id dedupe makes retries safe)
    assert {"gamevault", "juwa", "juwa2"}.isdisjoint(NON_IDEMPOTENT_DRIVERS)


def test_goldentreasure_driver_routes_to_goldentreasure_backend(fake_redis):
    s = _settings()
    backend = resolve_backend(
        "goldentreasure", credentials=_gt_creds(),
        http_client=object(), settings=s, redis=fake_redis,
    )
    assert isinstance(backend, GoldenTreasureBackend)


def test_goldentreasure_missing_credentials_raises():
    s = _settings()
    creds = GameCredentials(
        game_id=13, name="g",
        backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="goldentreasure",
    )
    with pytest.raises(BackendError) as ei:
        resolve_backend(
            "goldentreasure", credentials=creds,
            http_client=object(), settings=s, redis=object(),
        )
    assert ei.value.reason == "missing_goldentreasure_credentials"


def test_goldentreasure_missing_redis_raises():
    s = _settings()
    with pytest.raises(BackendError) as ei:
        resolve_backend(
            "goldentreasure", credentials=_gt_creds(),
            http_client=object(), settings=s, redis=None,
        )
    assert ei.value.reason == "missing_redis_client"


def test_orionstars_and_milkyway_in_non_idempotent_drivers():
    from app.backends.registry import NON_IDEMPOTENT_DRIVERS
    assert "orionstars" in NON_IDEMPOTENT_DRIVERS
    assert "milkyway" in NON_IDEMPOTENT_DRIVERS


async def test_resolve_orionstars_returns_orionstars_backend(fake_redis):
    import httpx
    from app.backends.context import GameCredentials
    from app.backends.orionstars.backend import OrionStarsBackend
    from app.backends.registry import resolve_backend
    from app.config import Settings
    creds = GameCredentials(
        game_id=99, name="OS",
        backend_url="https://os.test", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="orionstars",
    )
    settings = Settings(anticaptcha_api_key="testkey")
    async with httpx.AsyncClient() as http:
        b = resolve_backend(
            "orionstars", credentials=creds, http_client=http,
            settings=settings, redis=fake_redis,
        )
    assert isinstance(b, OrionStarsBackend)


async def test_resolve_milkyway_returns_milkyway_backend(fake_redis):
    import httpx
    from app.backends.context import GameCredentials
    from app.backends.milkyway.backend import MilkyWayBackend
    from app.backends.registry import resolve_backend
    from app.config import Settings
    creds = GameCredentials(
        game_id=100, name="MW",
        backend_url="https://mw.test", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="milkyway",
    )
    settings = Settings(anticaptcha_api_key="testkey")
    async with httpx.AsyncClient() as http:
        b = resolve_backend(
            "milkyway", credentials=creds, http_client=http,
            settings=settings, redis=fake_redis,
        )
    assert isinstance(b, MilkyWayBackend)


async def test_resolve_orionstars_requires_anticaptcha_key(fake_redis):
    import httpx
    from app.backends.base import BackendError
    from app.backends.context import GameCredentials
    from app.backends.registry import resolve_backend
    from app.config import Settings
    creds = GameCredentials(
        game_id=99, name="OS",
        backend_url="https://os.test", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="orionstars",
    )
    settings = Settings(anticaptcha_api_key="")
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError, match="missing_anticaptcha_api_key"):
            resolve_backend(
                "orionstars", credentials=creds, http_client=http,
                settings=settings, redis=fake_redis,
            )


def test_ultrapanda_and_vblink_in_non_idempotent_drivers():
    from app.backends.registry import NON_IDEMPOTENT_DRIVERS
    assert "ultrapanda" in NON_IDEMPOTENT_DRIVERS
    assert "vblink" in NON_IDEMPOTENT_DRIVERS


async def test_resolve_ultrapanda_returns_ultrapanda_backend(fake_redis):
    import httpx
    from app.backends.context import GameCredentials
    from app.backends.registry import resolve_backend
    from app.backends.ultrapanda.backend import UltraPandaBackend
    from app.config import Settings
    creds = GameCredentials(
        game_id=99, name="UP",
        backend_url="https://up.test", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="ultrapanda",
    )
    settings = Settings()
    async with httpx.AsyncClient() as http:
        b = resolve_backend(
            "ultrapanda", credentials=creds, http_client=http,
            settings=settings, redis=fake_redis,
        )
    assert isinstance(b, UltraPandaBackend)
    assert b._client._driver == "ultrapanda"


async def test_resolve_vblink_returns_ultrapanda_backend_with_vblink_prefix(fake_redis):
    """VBlink is a registry alias: same class, different driver_prefix."""
    import httpx
    from app.backends.context import GameCredentials
    from app.backends.registry import resolve_backend
    from app.backends.ultrapanda.backend import UltraPandaBackend
    from app.config import Settings
    creds = GameCredentials(
        game_id=100, name="VB",
        backend_url="https://vb.test", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="vblink",
    )
    settings = Settings()
    async with httpx.AsyncClient() as http:
        b = resolve_backend(
            "vblink", credentials=creds, http_client=http,
            settings=settings, redis=fake_redis,
        )
    assert isinstance(b, UltraPandaBackend)
    assert b._client._driver == "vblink"


async def test_resolve_ultrapanda_requires_credentials(fake_redis):
    import httpx
    import pytest
    from app.backends.base import BackendError
    from app.backends.context import GameCredentials
    from app.backends.registry import resolve_backend
    from app.config import Settings
    creds = GameCredentials(
        game_id=99, name="UP",
        backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="ultrapanda",
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError, match="missing_ultrapanda_credentials"):
            resolve_backend(
                "ultrapanda", credentials=creds, http_client=http,
                settings=Settings(), redis=fake_redis,
            )


async def test_resolve_ultrapanda_requires_redis():
    import httpx
    import pytest
    from app.backends.base import BackendError
    from app.backends.context import GameCredentials
    from app.backends.registry import resolve_backend
    from app.config import Settings
    creds = GameCredentials(
        game_id=99, name="UP",
        backend_url="https://up.test", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="ultrapanda",
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError, match="missing_redis_client"):
            resolve_backend(
                "ultrapanda", credentials=creds, http_client=http,
                settings=Settings(), redis=None,
            )


def test_firekirin_and_pandamaster_in_non_idempotent_drivers():
    from app.backends.registry import NON_IDEMPOTENT_DRIVERS
    assert "firekirin" in NON_IDEMPOTENT_DRIVERS
    assert "pandamaster" in NON_IDEMPOTENT_DRIVERS


async def test_resolve_firekirin_returns_milkyway_backend_with_firekirin_prefix(fake_redis):
    """Firekirin is a registry alias of MilkyWay: same class, different driver_prefix."""
    import httpx
    from app.backends.context import GameCredentials
    from app.backends.milkyway.backend import MilkyWayBackend
    from app.backends.registry import resolve_backend
    from app.config import Settings
    creds = GameCredentials(
        game_id=200, name="FK",
        backend_url="https://firekirin.xyz:8888", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="firekirin",
    )
    settings = Settings(anticaptcha_api_key="testkey")
    async with httpx.AsyncClient() as http:
        b = resolve_backend(
            "firekirin", credentials=creds, http_client=http,
            settings=settings, redis=fake_redis,
        )
    assert isinstance(b, MilkyWayBackend)
    assert b._client._driver == "firekirin"


async def test_resolve_pandamaster_returns_milkyway_backend_with_pandamaster_prefix(fake_redis):
    """Pandamaster is a registry alias of MilkyWay (runs on default 443, no port)."""
    import httpx
    from app.backends.context import GameCredentials
    from app.backends.milkyway.backend import MilkyWayBackend
    from app.backends.registry import resolve_backend
    from app.config import Settings
    creds = GameCredentials(
        game_id=201, name="PM",
        backend_url="https://pandamaster.vip", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="pandamaster",
    )
    settings = Settings(anticaptcha_api_key="testkey")
    async with httpx.AsyncClient() as http:
        b = resolve_backend(
            "pandamaster", credentials=creds, http_client=http,
            settings=settings, redis=fake_redis,
        )
    assert isinstance(b, MilkyWayBackend)
    assert b._client._driver == "pandamaster"


async def test_resolve_firekirin_requires_credentials(fake_redis):
    import httpx
    import pytest
    from app.backends.base import BackendError
    from app.backends.context import GameCredentials
    from app.backends.registry import resolve_backend
    from app.config import Settings
    creds = GameCredentials(
        game_id=200, name="FK",
        backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="firekirin",
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError, match="missing_firekirin_credentials"):
            resolve_backend(
                "firekirin", credentials=creds, http_client=http,
                settings=Settings(anticaptcha_api_key="testkey"), redis=fake_redis,
            )
