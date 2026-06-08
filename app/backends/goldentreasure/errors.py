GTREASURE_STATUS: dict[int, str] = {
    8: "account_exists",
    21: "operation_refused",        # over-limit / insufficient — misleading "server maintenance" msg
    52: "no_permission",            # surfaces only if relogin retry also fails
    167: "rate_limited",
    1003: "invalid_password_format",
    -3: "token_invalid",
    -17: "token_expired",
    30100: "system_verify_required",
    30200: "google_auth_bind_required",
    30201: "google_auth_verify_required",
}

# Codes the executor should NOT cache (a future system with max_tries>1 would retry these).
# With _max_tries=1, transient still fails the op once and Laravel's reaper handles it.
TRANSIENT_CODES: frozenset[int] = frozenset({167})


def map_response(code: int, message: str) -> tuple[str, bool]:
    """Return (reason_slug, is_terminal). Cache only terminal failures."""
    if code in TRANSIENT_CODES:
        return (f"gtreasure:{GTREASURE_STATUS[code]}", False)
    if code in GTREASURE_STATUS:
        return (f"gtreasure:{GTREASURE_STATUS[code]}", True)
    return (f"gtreasure:code_{code}: {(message or '')[:80]}", True)
