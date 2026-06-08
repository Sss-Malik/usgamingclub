from app.backends.goldentreasure.errors import map_response


def test_167_is_transient_rate_limited():
    reason, terminal = map_response(167, "high frequency request")
    assert reason == "gtreasure:rate_limited"
    assert terminal is False


def test_known_terminal_codes():
    cases = [
        (8, "gtreasure:account_exists"),
        (21, "gtreasure:operation_refused"),
        (52, "gtreasure:no_permission"),
        (1003, "gtreasure:invalid_password_format"),
        (-3, "gtreasure:token_invalid"),
        (-17, "gtreasure:token_expired"),
        (30100, "gtreasure:system_verify_required"),
        (30200, "gtreasure:google_auth_bind_required"),
        (30201, "gtreasure:google_auth_verify_required"),
    ]
    for code, expected in cases:
        reason, terminal = map_response(code, "msg")
        assert reason == expected, (code, reason)
        assert terminal is True


def test_unknown_code_truncates_message():
    msg = "x" * 200
    reason, terminal = map_response(9999, msg)
    assert reason.startswith("gtreasure:code_9999: ")
    assert len(reason) <= len("gtreasure:code_9999: ") + 80
    assert terminal is True
