from app.backends.gamevault.errors import GAMEVAULT_STATUS, TRANSIENT_CODES, map_code


def test_known_code_maps_to_slug():
    slug, code, msg = map_code(6, "Insufficient agent balance")
    assert slug == "gamevault:6:insufficient_agent_balance"
    assert code == 6
    assert msg == "Insufficient agent balance"

    slug, code, msg = map_code(10, "x")
    assert slug == "gamevault:10:user_in_game"

    slug, code, msg = map_code(20, "x")
    assert slug == "gamevault:20:account_exists"


def test_unknown_code_falls_back_to_msg():
    slug, code, msg = map_code(999, "weird error")
    assert slug == "gamevault:999:weird error"
    assert code == 999
    assert msg == "weird error"


def test_transient_codes_are_recharge_withdraw_system():
    assert TRANSIENT_CODES == {12, 14, 21}


def test_dictionary_covers_documented_codes():
    for code in list(range(1, 24)) + [400]:
        assert code in GAMEVAULT_STATUS


def test_map_code_returns_slug_code_and_message():
    slug, code, msg = map_code(7, "user balance not enough")
    assert slug == "gamevault:7:insufficient_user_balance"
    assert code == 7
    assert msg == "user balance not enough"


def test_map_code_unknown_keeps_raw_message_untruncated():
    long = "x" * 200
    slug, code, msg = map_code(999, long)
    assert code == 999
    assert msg == long            # untruncated in the structured field
    assert slug.startswith("gamevault:999:")
