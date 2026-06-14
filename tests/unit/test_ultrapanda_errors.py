from app.backends.ultrapanda.errors import map_code


def test_success_code_returns_none():
    # 20000 is success; the mapper is only called on non-success, but defensively returns None.
    assert map_code(20000, op="recharge") is None


def test_login_bad_credentials():
    slug, terminal = map_code(5, op="login")
    assert slug == "bad_credentials"
    assert terminal is True


def test_create_duplicate_account():
    slug, terminal = map_code(8, op="create_account")
    assert slug == "account_exists"
    assert terminal is True


def test_recharge_insufficient_agent_funds():
    slug, terminal = map_code(21, op="recharge")
    assert slug == "insufficient_agent_funds"
    assert terminal is True


def test_redeem_insufficient_player_credit():
    slug, terminal = map_code(21, op="redeem")
    assert slug == "insufficient_player_credit"
    assert terminal is True


def test_player_not_found_on_recharge():
    slug, terminal = map_code(22, op="recharge")
    assert slug == "player_not_found"
    assert terminal is True


def test_no_permission():
    slug, terminal = map_code(52, op="recharge")
    assert slug == "no_permission"
    assert terminal is True


def test_rate_limit_is_transient():
    slug, terminal = map_code(167, op="recharge")
    assert slug == "rate_limited"
    assert terminal is False


def test_invalid_chars_on_create():
    slug, terminal = map_code(1003, op="create_account")
    assert slug == "account_invalid_chars"
    assert terminal is True


def test_session_expired_is_transient():
    slug, terminal = map_code(1086, op="recharge")
    assert slug == "session_expired"
    assert terminal is False


def test_unknown_code_returns_terminal_unknown_slug():
    slug, terminal = map_code(99999, op="recharge")
    assert slug == "unknown:99999"
    assert terminal is True
