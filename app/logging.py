# app/logging.py
import logging
import sys

import structlog

from app.config import get_settings

SECRET_KEYS = {
    "password",
    "backend_password",
    "api_secret_key",
    "binding_key",
    "secret",
    "x-signature",
}


def redact_processor(_logger, _name, event_dict):
    for key in list(event_dict.keys()):
        if key.lower() in SECRET_KEYS and event_dict[key] is not None:
            event_dict[key] = "***"
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
        wrapper_class=structlog.make_filtered_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
