"""Code → (slug, is_terminal) mapping for the vpower (UltraPanda/VBlink) backend.

Only codes actually observed live against the test backends are mapped (see findings §4).
Frontend-dictionary-only codes (§5) are intentionally NOT mapped — they don't appear in
real API responses.
"""


def map_code(code: int, *, op: str) -> tuple[str, bool] | None:
    """Translate a vpower response code into a (reason_slug, is_terminal) pair.

    Returns None for `code == 20000` (success). The `op` argument lets us disambiguate
    code 21 (the server returns the same generic message for over-recharge agent-side and
    over-withdraw player-side).
    """
    if code == 20000:
        return None
    if code == 5:
        return ("bad_credentials", True)
    if code == 8:
        return ("account_exists", True)
    if code == 21:
        if op == "recharge":
            return ("insufficient_agent_funds", True)
        if op == "redeem":
            return ("insufficient_player_credit", True)
        return ("unknown_21", True)
    if code == 22:
        return ("player_not_found", True)
    if code == 52:
        return ("no_permission", True)
    if code == 167:
        return ("rate_limited", False)
    if code == 1003:
        return ("account_invalid_chars", True)
    if code == 1086:
        return ("session_expired", False)
    return (f"unknown:{code}", True)
