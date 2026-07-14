from app.backends.goldentreasure.errors import map_response


def test_167_is_transient_rate_limited():
    reason, terminal, code, msg = map_response(167, "high frequency request")
    assert reason == "gtreasure:rate_limited"
    assert terminal is False
    assert code == 167
    assert msg == "high frequency request"


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
        reason, terminal, resp_code, msg = map_response(code, "msg")
        assert reason == expected, (code, reason)
        assert terminal is True
        assert resp_code == code
        assert msg == "msg"


def test_unknown_code_truncates_message():
    msg = "x" * 200
    reason, terminal, code, raw_msg = map_response(9999, msg)
    assert reason.startswith("gtreasure:code_9999: ")
    assert len(reason) <= len("gtreasure:code_9999: ") + 80
    assert terminal is True
    assert code == 9999
    assert raw_msg == msg                      # untruncated, unlike the slug


def test_map_response_returns_code_and_message():
    slug, terminal, code, msg = map_response(21, "server maintenance")
    assert slug == "gtreasure:operation_refused"
    assert terminal is True
    assert code == 21
    assert msg == "server maintenance"


def test_transient_167_returns_code():
    slug, terminal, code, msg = map_response(167, "too fast")
    assert terminal is False and code == 167
