# app/backends/gamevault/errors.py

GAMEVAULT_STATUS: dict[int, str] = {
    1: "invalid_agent_id",
    2: "invalid_request_parameters",
    3: "invalid_token",
    4: "token_expired",
    5: "ip_not_whitelisted",
    6: "insufficient_agent_balance",
    7: "insufficient_user_balance",
    8: "invalid_user_id",
    9: "user_account_frozen",
    10: "user_in_game",
    11: "invalid_amount",
    12: "recharge_failed",
    13: "recharge_permission_denied",
    14: "withdrawal_failed",
    15: "withdrawal_exceeds_daily_limit",
    16: "withdrawal_under_review",
    17: "withdrawal_permission_denied",
    18: "account_name_format_error",
    19: "agent_no_register_permission",
    20: "account_exists",
    21: "system_failed",
    22: "register_ip_limit",
    23: "password_length",
    400: "parameter_error",
}

# Codes treated as transient/retryable (not cached; safe to re-run thanks to order_id dedupe).
TRANSIENT_CODES: frozenset[int] = frozenset({12, 14, 21})


def map_code(code: int, msg: str) -> tuple[str, int, str]:
    """Map a GameVault status code to (slug, code, raw_msg).

    `slug` truncates unknown messages to 80 chars for the player-facing reason string;
    `raw_msg` is the untruncated message, carried separately for the provider diagnostics field.
    """
    slug = GAMEVAULT_STATUS.get(code)
    raw = msg or ""
    if slug is not None:
        return (f"gamevault:{code}:{slug}", code, raw)
    return (f"gamevault:{code}:{raw[:80] or 'error'}", code, raw)
