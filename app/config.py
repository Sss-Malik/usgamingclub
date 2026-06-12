# app/config.py
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    env: str = "development"
    log_level: str = "INFO"

    python_signing_secret: str = ""
    app_url: str = "http://127.0.0.1:8000"

    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_name: str = ""
    db_user: str = ""
    db_password: str = ""
    # SQLAlchemy async MySQL driver. Prod/Docker: "asyncmy" (compiled, fast). Local dev without
    # Docker can use "aiomysql" (pure-Python, no build step).
    db_driver: str = "asyncmy"

    redis_url: str = "redis://127.0.0.1:6379/0"

    webhook_max_budget_seconds: float = 600.0
    webhook_backoff_base: float = 0.5
    webhook_backoff_max: float = 30.0

    result_cache_ttl_seconds: int = 900

    mock_force_fail: bool = False
    mock_force_fail_reason: str = "forced mock failure"

    anticaptcha_api_key: str = ""
    anticaptcha_poll_interval_seconds: float = 2.0
    anticaptcha_max_poll_seconds: float = 120.0
    captcha_login_max_attempts: int = 3
    aspnet_session_ttl_seconds: int = 1800
    aspnet_lock_ttl_seconds: int = 20
    aspnet_lock_acquire_timeout_seconds: float = 30.0
    vpower_session_ttl_seconds: int = 1800
    vpower_throttle_ttl_seconds: int = 6
    vpower_throttle_acquire_timeout_seconds: float = 10.0
    vpower_session_lock_ttl_seconds: int = 10
    vpower_session_lock_acquire_timeout_seconds: float = 10.0

    replay_window_seconds: int = 300

    @property
    def webhook_url(self) -> str:
        return f"{self.app_url.rstrip('/')}/webhooks/games/operation"

    @property
    def ping_url(self) -> str:
        return f"{self.app_url.rstrip('/')}/webhooks/_ping"

    @property
    def db_dsn(self) -> str:
        return (
            f"mysql+{self.db_driver}://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def require_runtime_settings(settings: Settings) -> None:
    """Fail fast on misconfiguration at service startup.

    Without the shared secret, every inbound trigger 401s and every outbound webhook
    is rejected — a silent, confusing failure. Surface it loudly at boot instead.
    """
    if not settings.python_signing_secret:
        raise RuntimeError(
            "PYTHON_SIGNING_SECRET is not set — inbound triggers and outbound webhooks "
            "cannot be authenticated. Configure it to match Laravel."
        )
