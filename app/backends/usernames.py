# app/backends/usernames.py
import re
import secrets

_MAX_BASE = 12
_SUFFIX_DIGITS = 3  # base(<=12) + 3 digits = <=15 total, safe across unknown backends


def _sanitize_base(value: str | None) -> str:
    """Lowercase, keep only [a-z0-9], truncate to the safe base length."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())[:_MAX_BASE]


def generate_username(full_name: str | None = None, *, provided: str | None = None) -> str:
    """Provider-safe username.

    A player-`provided` base wins; otherwise derive from `full_name`. Both are sanitized to
    lowercase alphanumerics and capped at `_MAX_BASE`. Falls back to `user` when nothing usable
    remains. Always appends `_SUFFIX_DIGITS` random digits for cross-player uniqueness, keeping the
    total <= 15 — a conservative cap, since we do not know each backend's real username limit.

    This function does NOT trust its input: it re-sanitizes and re-caps regardless of what the
    caller (Arcadia) validated, so it is the last line of defense at the provider boundary.
    """
    base = _sanitize_base(provided) or _sanitize_base(full_name) or "user"
    suffix = f"{secrets.randbelow(10 ** _SUFFIX_DIGITS):0{_SUFFIX_DIGITS}d}"
    return f"{base}{suffix}"
