# app/logging.py
import logging
import sys

import structlog

from app.config import get_settings

SECRET_KEYS = {
    "password",
    "pwd",                  # Golden Treasure: plaintext player password in savePlayer/updatePlayer bodies
    "login_pwd",
    "backend_password",
    "api_secret_key",
    "binding_key",
    "secret",
    "token",
    "x-signature",
    "x-token",              # Golden Treasure: AES of session token; per-request rebuild
    # Phase 5: ASP.NET cashier form fields + session cookie + AntiCaptcha key
    "txtloginpass",
    "txtlogonpass",
    "txtlogonpass2",
    "txtconfirmpass",
    "txtsureconfirmpass",
    "asp.net_sessionid",
    "anticaptcha_api_key",
    # Phase 6: UltraPanda/VBlink (vpower)
    "admin-token",
    "auth_code",
    # Phase 7: Arcadia integration
    "new_password",
    "account_created",
    "user_data",
    "amount",
    "api_secret",
    "webhook_secret",
    "x-webhook-signature",
    "x-request-signature",
    # Phase 8: YOLO777 session cookies + CSRF token
    "_token",
    "csrf_token",
    "laravel_session",
    "xsrf-token",
}


def _redact_in_place(d: dict) -> None:
    for key in list(d.keys()):
        value = d[key]
        if key.lower() in SECRET_KEYS and value is not None:
            d[key] = "***"
        elif isinstance(value, dict):
            _redact_in_place(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _redact_in_place(item)


def redact_processor(_logger, _name, event_dict):
    # Redact recursively: a secret nested in a logged dict (e.g. credentials) must
    # never leak, not just top-level keys.
    _redact_in_place(event_dict)
    return event_dict


def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
