from app.backends.gameroom.errors import map_response


def test_500_is_transient():
    reason, terminal = map_response(500, "Service exception")
    assert reason == "gameroom:server_error"
    assert terminal is False


def test_430_is_terminal_auth_failed():
    reason, terminal = map_response(430, "Username or password error")
    assert reason == "gameroom:auth_failed"
    assert terminal is True


def test_401_is_transient_auth_missing():
    reason, terminal = map_response(401, "Token not provided")
    assert reason == "gameroom:auth_missing"
    assert terminal is False


def test_400_message_patterns_to_terminal_slugs():
    cases = [
        ("Username already exists", "gameroom:account_exists"),
        ("Recharge balance is greater than available balance, please check and recharge again", "gameroom:insufficient_agent_balance"),
        ("Withdrawal amount is greater than customer balance. Please check and withdraw again", "gameroom:insufficient_user_balance"),
        ("Amount must be greater than 0", "gameroom:invalid_amount"),
        ("The balance must be an integer.", "gameroom:invalid_amount"),
        ("The password confirmation does not match.", "gameroom:password_mismatch"),
        ("Operation failed", "gameroom:operation_failed"),
    ]
    for msg, expected in cases:
        reason, terminal = map_response(400, msg)
        assert reason == expected, (msg, reason)
        assert terminal is True


def test_400_unknown_message_is_terminal_business_error_truncated():
    msg = "x" * 200
    reason, terminal = map_response(400, msg)
    assert reason.startswith("gameroom:business_error: ")
    assert len(reason) <= len("gameroom:business_error: ") + 80
    assert terminal is True


def test_other_status_is_terminal_with_slug():
    reason, terminal = map_response(403, "Forbidden")
    assert reason == "gameroom:status_403: Forbidden"
    assert terminal is True
