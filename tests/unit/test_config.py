# tests/unit/test_config.py
import importlib
import app.config as config_module


def _fresh_settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    importlib.reload(config_module)
    config_module.get_settings.cache_clear()
    return config_module.get_settings()


def test_settings_read_from_env_and_build_urls(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        PYTHON_SIGNING_SECRET="secret",
        APP_URL="https://laravel.test/",
        DB_NAME="casino",
        DB_USER="ro",
        DB_PASSWORD="pw",
        DB_HOST="db",
        DB_PORT="3307",
    )
    assert s.python_signing_secret == "secret"
    assert s.webhook_url == "https://laravel.test/webhooks/games/operation"
    assert s.ping_url == "https://laravel.test/webhooks/_ping"
    assert s.db_dsn == "mysql+asyncmy://ro:pw@db:3307/casino"
    assert s.replay_window_seconds == 300
