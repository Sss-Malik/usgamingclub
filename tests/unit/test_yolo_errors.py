import pytest

from app.backends.base import BackendError, TransientBackendError
from app.backends.yolo.errors import looks_like_auth_failure, map_envelope


def test_success_returns_data():
    body = {"status": True, "data": {"message": "success", "type": "success"}}
    assert map_envelope(200, body) == {"message": "success", "type": "success"}


def test_business_error_insufficient_terminal():
    body = {"status": False, "data": {"message": "The score is insufficient", "type": "error"}}
    with pytest.raises(BackendError, match="yolo:insufficient_balance"):
        map_envelope(200, body)


def test_validation_account_exists_terminal():
    body = {"status": False, "data": [], "errors": {"Accounts": ["The Accounts has already been taken."]}}
    with pytest.raises(BackendError, match="yolo:account_exists"):
        map_envelope(422, body)


def test_validation_password_too_short_terminal():
    body = {"status": False, "data": [], "errors": {"password": ["The Password must be at least 6 characters."]}}
    with pytest.raises(BackendError, match="yolo:too_short"):
        map_envelope(422, body)


def test_server_error_transient():
    with pytest.raises(TransientBackendError):
        map_envelope(500, None)


def test_non_json_transient():
    with pytest.raises(TransientBackendError):
        map_envelope(200, None)


def test_auth_failure_detection():
    assert looks_like_auth_failure(401, "", "") is True
    assert looks_like_auth_failure(419, "", "") is True
    assert looks_like_auth_failure(302, "https://x/admin/auth/login", "") is True
    assert looks_like_auth_failure(200, "", "") is False
