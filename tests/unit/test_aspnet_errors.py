from app.backends._aspnet_cashier.errors import (
    LOGIN_ERRTYPE_MESSAGES,
    classify_business_failure_message,
    login_errtype_to_code,
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
