# tests/unit/test_logging.py
from app.logging import redact_processor, SECRET_KEYS


def test_redact_masks_secret_keys():
    event = {"event": "x", "backend_password": "p", "api_secret_key": "k", "balance_cents": 10}
    out = redact_processor(None, None, event)
    assert out["backend_password"] == "***"
    assert out["api_secret_key"] == "***"
    assert out["balance_cents"] == 10


def test_password_is_a_secret_key():
    assert "password" in SECRET_KEYS
