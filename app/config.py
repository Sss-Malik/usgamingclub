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

    redis_url: str = "redis://127.0.0.1:6379/0"

    webhook_max_budget_seconds: float = 600.0
    webhook_backoff_base: float = 0.5
    webhook_backoff_max: float = 30.0

    result_cache_ttl_seconds: int = 900

    mock_force_fail: bool = False
    mock_force_fail_reason: str = "forced mock failure"

    anticaptcha_api_key: str = ""

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
            f"mysql+asyncmy://{self.db_user}:{self.db_password}"
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
