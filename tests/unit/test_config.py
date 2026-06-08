# tests/unit/test_config.py
import importlib

import pytest

import app.config as config_module
from app.config import Settings, require_runtime_settings


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
        DB_DRIVER="asyncmy",  # pin explicitly so a local .env can't change the asserted DSN
    )
    assert s.python_signing_secret == "secret"
    assert s.webhook_url == "https://laravel.test/webhooks/games/operation"
    assert s.ping_url == "https://laravel.test/webhooks/_ping"
    assert s.db_dsn == "mysql+asyncmy://ro:pw@db:3307/casino"
    assert s.replay_window_seconds == 300


def test_require_runtime_settings_rejects_empty_secret():
    with pytest.raises(RuntimeError):
        require_runtime_settings(Settings(python_signing_secret=""))
    # a configured secret passes without raising
    require_runtime_settings(Settings(python_signing_secret="configured"))


def test_captcha_and_aspnet_session_defaults():
    from app.config import Settings

    s = Settings()
    assert s.anticaptcha_poll_interval_seconds == 2.0
    assert s.anticaptcha_max_poll_seconds == 120.0
    assert s.captcha_login_max_attempts == 3
    assert s.aspnet_session_ttl_seconds == 1800
    assert s.aspnet_lock_ttl_seconds == 20
    assert s.aspnet_lock_acquire_timeout_seconds == 30.0


def test_db_driver_override_changes_dsn(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        PYTHON_SIGNING_SECRET="x",
        DB_NAME="casino",
        DB_USER="root",
        DB_HOST="127.0.0.1",
        DB_PORT="3306",
        DB_DRIVER="aiomysql",
    )
    assert s.db_dsn == "mysql+aiomysql://root:@127.0.0.1:3306/casino"
    assert s.db_driver == "aiomysql"
