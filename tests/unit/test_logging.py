# tests/unit/test_logging.py
from app.logging import configure_logging, get_logger, redact_processor, SECRET_KEYS


def test_redact_masks_secret_keys():
    event = {"event": "x", "backend_password": "p", "api_secret_key": "k", "balance_cents": 10}
    out = redact_processor(None, None, event)
    assert out["backend_password"] == "***"
    assert out["api_secret_key"] == "***"
    assert out["balance_cents"] == 10


def test_password_is_a_secret_key():
    assert "password" in SECRET_KEYS


def test_redact_is_recursive():
    event = {"event": "x", "creds": {"api_secret_key": "k", "name": "ok"}}
    out = redact_processor(None, None, event)
    assert out["creds"]["api_secret_key"] == "***"
    assert out["creds"]["name"] == "ok"


def test_configure_logging_runs_and_logger_emits(capsys):
    # Regression: configure_logging() runs on app/worker startup. A bad structlog
    # API name here would crash the service at boot even though the redaction unit
    # tests still pass, so exercise the real configuration path.
    configure_logging()
    get_logger("test").info("hello", api_secret_key="should-be-redacted")
    out = capsys.readouterr().out
    assert "hello" in out
    assert "should-be-redacted" not in out
    assert "***" in out
