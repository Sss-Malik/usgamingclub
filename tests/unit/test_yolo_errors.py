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


def test_validation_account_invalid_terminal():
    body = {"status": False, "data": [], "errors": {"Accounts": ["The Accounts format is invalid."]}}
    with pytest.raises(BackendError, match="yolo:account_invalid"):
        map_envelope(422, body)


def test_validation_field_required_terminal():
    body = {"status": False, "data": [], "errors": {"password": ["The Password field is required."]}}
    with pytest.raises(BackendError, match="yolo:field_required"):
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


def test_auth_failure_via_login_page_body():
    login_page = '<form action="/admin/auth/login"><input name="password" type="password"></form>'
    assert looks_like_auth_failure(200, "", login_page) is True


# ---- diagnostics: provider fields on map_envelope raises ----

def test_business_error_carries_http_status_and_untruncated_message():
    long = "score is insufficient " + "x" * 200
    with pytest.raises(BackendError) as ei:
        map_envelope(200, {"status": False, "data": {"message": long}})
    err = ei.value
    assert err.provider_http_status == 200
    assert err.provider_message == long        # untruncated
    assert err.provider_code is None
    assert err.reason == "yolo:insufficient_balance"


def test_validation_error_carries_http_status_and_untruncated_message():
    long = "The Accounts has already been taken. " + "x" * 200
    with pytest.raises(BackendError) as ei:
        map_envelope(422, {"status": False, "data": [], "errors": {"Accounts": [long]}})
    err = ei.value
    assert err.provider_http_status == 422
    assert err.provider_message == long        # untruncated
    assert err.provider_code is None
    assert err.reason == "yolo:account_exists"


def test_unmatched_validation_error_still_carries_provider_fields():
    with pytest.raises(BackendError) as ei:
        map_envelope(422, {"status": False, "data": [], "errors": {"weird": ["Some odd rule."]}})
    err = ei.value
    assert err.provider_http_status == 422
    assert err.provider_message == "Some odd rule."
    assert err.provider_code is None


def test_unmatched_business_error_still_carries_provider_fields():
    with pytest.raises(BackendError) as ei:
        map_envelope(200, {"status": False, "data": {"message": "Some unmapped failure"}})
    err = ei.value
    assert err.provider_http_status == 200
    assert err.provider_message == "Some unmapped failure"
    assert err.provider_code is None


def test_server_error_carries_http_status():
    with pytest.raises(TransientBackendError) as ei:
        map_envelope(503, None)
    assert ei.value.provider_http_status == 503
    assert ei.value.provider_code is None


def test_bad_response_carries_http_status():
    with pytest.raises(TransientBackendError) as ei:
        map_envelope(200, None)
    assert ei.value.provider_http_status == 200
