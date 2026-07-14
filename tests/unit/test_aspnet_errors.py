import pytest

from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.errors import (
    LOGIN_ERRTYPE_MESSAGES,
    classify_business_failure_message,
    login_errtype_to_code,
)
from app.backends.base import BackendError
from tests.conftest import FakeCaptchaSolver


@pytest.fixture
def aspnet_client() -> AspnetCashierClient:
    """A client with no reachable HTTP/session store — only used for the pure
    message-mapping methods (business_failure_to_error, classify), which never
    touch the network."""
    return AspnetCashierClient(
        base_url="http://x", username="u", password="p",
        http_client=object(),
        session_store=object(),
        captcha_solver=FakeCaptchaSolver(),
        game_id=1, session_ttl_seconds=1, lock_ttl_seconds=1,
        lock_acquire_timeout_seconds=1.0, captcha_login_max_attempts=1,
        driver_prefix="orionstars",
    )


def test_login_errtype_known_codes_map_to_short_codes():
    assert login_errtype_to_code("verifycode") == "captcha_wrong"
    assert login_errtype_to_code("overtime") == "session_overtime"
    assert login_errtype_to_code("errorNamePassowrd") == "bad_credentials"
    assert login_errtype_to_code("errorBlockIPErr") == "ip_blocked"
    assert login_errtype_to_code("errorBindIP") == "ip_not_bound"
    assert login_errtype_to_code("errorNullity") == "account_banned"
    assert login_errtype_to_code("errUser") == "session_stolen"


def test_login_errtype_unknown_falls_through_to_passthrough():
    assert login_errtype_to_code("totally_made_up") == "unknown:totally_made_up"


def test_login_errtype_table_keys_match_findings_doc_dictionary():
    # Confidence check: keys we mapped exist in the documented dictionary.
    for k in ["verifycode", "overtime", "errorNamePassowrd", "errorBindIP", "errUser"]:
        assert k in LOGIN_ERRTYPE_MESSAGES


def test_business_failure_messages_recharge_redeem_create_reset():
    assert classify_business_failure_message(
        "Sorry, the surplus money is insufficient!"
    ) == "insufficient_agent_funds"
    assert classify_business_failure_message(
        "Sorry, there is not enough gold for the operator!"
    ) == "insufficient_player_credit"
    assert classify_business_failure_message(
        "The account number already exists, please re-enter it!"
    ) == "account_exists"
    assert classify_business_failure_message(
        "account name should be compose with letters letters, underscore & numbers."
    ) == "account_invalid_chars"
    assert classify_business_failure_message(
        "entered passwords differ from the another."
    ) == "password_mismatch"
    assert classify_business_failure_message(
        "Inconsistent passwords entered"
    ) == "password_mismatch"


def test_business_failure_unknown_message_returns_unknown_slug():
    out = classify_business_failure_message("Some surprise message we have not seen")
    assert out.startswith("unknown:")


# --- BackendError provider_message/provider_code carried by the client's mappers ---

def test_business_failure_to_error_sets_provider_message(aspnet_client):
    msg = "surplus money is insufficient for this operation"
    err = aspnet_client.business_failure_to_error(msg)
    assert err.provider_message == msg          # untruncated
    assert err.provider_code is None
    assert err.reason == "orionstars:insufficient_agent_funds"  # driver_prefix from fixture


def test_business_failure_to_error_keeps_full_message_even_when_unknown(aspnet_client):
    msg = "a completely novel sentinel message the app has never classified before, long enough to exceed sixty characters"
    err = aspnet_client.business_failure_to_error(msg)
    assert err.provider_message == msg          # untruncated even though the slug itself is truncated
    assert err.reason == f"orionstars:unknown:{msg[:60]}"


def test_classify_unknown_sentinel_raises_without_provider_message(aspnet_client):
    # The unknown-sentinel branch must NOT leak the raw (untruncated, arbitrary third-party
    # HTML) response body into diagnostics.provider.message — redaction-safe by construction.
    html = "<html>some totally unrecognized page body that isn't a known sentinel</html>"
    with pytest.raises(BackendError) as ei:
        aspnet_client.classify(html)
    assert ei.value.provider_message is None
    assert html[:80] in ei.value.reason
