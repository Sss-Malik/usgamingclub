# Best-effort mapping from the findings doc §2.6 message-id dictionary to short error codes
# our system surfaces. Several entries (everything except verifycode and overtime) had their
# exact `errtype` query value inferred rather than individually exercised — confirm in the wild.
LOGIN_ERRTYPE_MESSAGES: dict[str, str] = {
    "verifycode":          "captcha_wrong",
    "overtime":            "session_overtime",
    "errorNamePassowrd":   "bad_credentials",
    "errorUserName":       "bad_username",
    "errorBlockIPErr":     "ip_blocked",
    "errorBindIP":         "ip_not_bound",
    "errorNullity":        "account_banned",
    "errorLogonTimeout":   "logon_timeout",
    "errorAuthParam":      "auth_param",
    "errorUnknown":        "server_unknown",
    "frequent":            "rate_limited",
    "errUser":             "session_stolen",
    "errorPassowrdTooLong":"password_too_long",
    "errorUserRole":       "not_admin",
}


def login_errtype_to_code(errtype: str) -> str:
    """Translate the `errtype` query value from a login 301 into a short, stable error code."""
    return LOGIN_ERRTYPE_MESSAGES.get(errtype, f"unknown:{errtype}")


# Substring-based classifier for the sentinel-string business failures. Mirrors §5.1 of the
# findings doc verbatim (typos and all — "letters letters", "differ from the another").
_BUSINESS_FAILURE_PATTERNS: list[tuple[str, str]] = [
    ("surplus money is insufficient",               "insufficient_agent_funds"),
    ("not enough gold for the operator",            "insufficient_player_credit"),
    ("account number already exists",               "account_exists"),
    ("account name should be compose",              "account_invalid_chars"),
    ("entered passwords differ",                    "password_mismatch"),
    ("inconsistent passwords entered",              "password_mismatch"),
]


def classify_business_failure_message(message: str) -> str:
    """Map a sentinel message string to a short, stable error slug.

    Returns "unknown:<truncated>" when no known pattern matches — the caller surfaces this so
    we notice and add a mapping.
    """
    low = (message or "").lower()
    for needle, slug in _BUSINESS_FAILURE_PATTERNS:
        if needle in low:
            return slug
    return f"unknown:{message[:60]}"
