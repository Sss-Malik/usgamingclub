# tests/unit/test_registry.py
from app.backends.mock.backend import MockBackend
from app.backends.registry import get_backend
from app.config import get_settings


def test_registry_returns_mock_backend_phase1():
    get_settings.cache_clear()
    backend = get_backend(7)
    assert isinstance(backend, MockBackend)


def test_registry_honors_force_fail(monkeypatch):
    monkeypatch.setenv("MOCK_FORCE_FAIL", "true")
    monkeypatch.setenv("MOCK_FORCE_FAIL_REASON", "manual")
    get_settings.cache_clear()
    backend = get_backend(7)
    assert backend._fail is True and backend._fail_reason == "manual"
    get_settings.cache_clear()
