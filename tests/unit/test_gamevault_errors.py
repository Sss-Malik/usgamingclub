from app.backends.gamevault.errors import GAMEVAULT_STATUS, TRANSIENT_CODES, map_code


def test_known_code_maps_to_slug():
    assert map_code(6, "Insufficient agent balance") == "gamevault:6:insufficient_agent_balance"
    assert map_code(10, "x") == "gamevault:10:user_in_game"
    assert map_code(20, "x") == "gamevault:20:account_exists"


def test_unknown_code_falls_back_to_msg():
    assert map_code(999, "weird error") == "gamevault:999:weird error"


def test_transient_codes_are_recharge_withdraw_system():
    assert TRANSIENT_CODES == {12, 14, 21}


def test_dictionary_covers_documented_codes():
    for code in list(range(1, 24)) + [400]:
        assert code in GAMEVAULT_STATUS
