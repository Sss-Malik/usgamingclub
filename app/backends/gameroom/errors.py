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


def map_response(status_code: int, message: str) -> tuple[str, bool, int, str]:
    """Map a Gameroom envelope (status_code + message) to (slug, terminal, status, raw_message).

    Terminal = same call would fail the same way; the executor caches these so a re-run
    short-circuits. Transient = retry-worthy (network / 5xx / 401-with-no-token).
    `status` and `raw_message` are the untruncated envelope status_code + message, carried
    for the webhook `diagnostics.provider` channel (never used for the player-facing slug,
    which may itself be truncated for unrecognized business errors).
    """
    msg = message or ""
    if status_code == 500:
        return ("gameroom:server_error", False, status_code, msg)
    if status_code == 430:
        return ("gameroom:auth_failed", True, status_code, msg)
    if status_code == 401:
        return ("gameroom:auth_missing", False, status_code, msg)
    if status_code == 400:
        low = msg.lower()
        for needle, slug in _MESSAGE_PATTERNS:
            if needle in low:
                return (f"gameroom:{slug}", True, status_code, msg)
        return (f"gameroom:business_error: {msg[:80]}", True, status_code, msg)
    return (f"gameroom:status_{status_code}: {msg[:60]}", True, status_code, msg)
