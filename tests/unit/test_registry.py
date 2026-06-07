# tests/unit/test_registry.py
import pytest

from app.backends.base import BackendError
from app.backends.context import GameCredentials
from app.backends.gamevault.backend import GameVaultBackend
from app.backends.mock.backend import MockBackend
from app.backends.registry import resolve_backend
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
