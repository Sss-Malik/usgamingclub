# Substring patterns from the findings doc §4.5-4.8. Matched case-insensitively.
_MESSAGE_PATTERNS: list[tuple[str, str]] = [
    ("username already exists", "account_exists"),
    ("recharge balance is greater", "insufficient_agent_balance"),
    ("withdrawal amount is greater", "insufficient_user_balance"),
    ("amount must be greater than 0", "invalid_amount"),
    ("balance must be an integer", "invalid_amount"),
    ("password confirmation does not match", "password_mismatch"),
    ("operation failed", "operation_failed"),
]


def map_response(status_code: int, message: str) -> tuple[str, bool]:
    """Map a Gameroom envelope (status_code + message) to (reason_slug, is_terminal).

    Terminal = same call would fail the same way; the executor caches these so a re-run
    short-circuits. Transient = retry-worthy (network / 5xx / 401-with-no-token).
    """
    msg = message or ""
    if status_code == 500:
        return ("gameroom:server_error", False)
    if status_code == 430:
        return ("gameroom:auth_failed", True)
    if status_code == 401:
        return ("gameroom:auth_missing", False)
    if status_code == 400:
        low = msg.lower()
        for needle, slug in _MESSAGE_PATTERNS:
            if needle in low:
                return (f"gameroom:{slug}", True)
        return (f"gameroom:business_error: {msg[:80]}", True)
    return (f"gameroom:status_{status_code}: {msg[:60]}", True)
