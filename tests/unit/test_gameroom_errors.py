from app.backends.gameroom.errors import map_response


def test_500_is_transient():
    slug, terminal, status, msg = map_response(500, "Service exception")
    assert slug == "gameroom:server_error"
    assert terminal is False
    assert status == 500
    assert msg == "Service exception"


def test_430_is_terminal_auth_failed():
    slug, terminal, status, msg = map_response(430, "Username or password error")
    assert slug == "gameroom:auth_failed"
    assert terminal is True
    assert status == 430
    assert msg == "Username or password error"


def test_401_is_transient_auth_missing():
    slug, terminal, status, msg = map_response(401, "Token not provided")
    assert slug == "gameroom:auth_missing"
    assert terminal is False
    assert status == 401
    assert msg == "Token not provided"


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
        slug, terminal, status, raw_msg = map_response(400, msg)
        assert slug == expected, (msg, slug)
        assert terminal is True
        assert status == 400
        assert raw_msg == msg


def test_400_unknown_message_is_terminal_business_error_truncated():
    msg = "x" * 200
    slug, terminal, status, raw_msg = map_response(400, msg)
    assert slug.startswith("gameroom:business_error: ")
    assert len(slug) <= len("gameroom:business_error: ") + 80
    assert terminal is True
    assert status == 400
    assert raw_msg == msg                      # untruncated, unlike the slug


def test_other_status_is_terminal_with_slug():
    slug, terminal, status, msg = map_response(403, "Forbidden")
    assert slug == "gameroom:status_403: Forbidden"
    assert terminal is True
    assert status == 403
    assert msg == "Forbidden"


def test_map_response_returns_envelope_status_and_message():
    slug, terminal, status, msg = map_response(400, "withdrawal amount is greater than balance")
    assert slug == "gameroom:insufficient_user_balance"
    assert terminal is True
    assert status == 400
    assert msg == "withdrawal amount is greater than balance"
