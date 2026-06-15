# app/backends/usernames.py
import re
import secrets

_MAX_BASE = 12


def generate_username(full_name: str) -> str:
    """Provider-safe username derived from a display name.

    Lowercase alphanumerics from `full_name` (truncated), plus 4 random digits for
    uniqueness. Falls back to `user` when the name yields nothing usable.
    """
    base = re.sub(r"[^a-z0-9]", "", (full_name or "").lower())[:_MAX_BASE]
    if not base:
        base = "user"
    suffix = f"{secrets.randbelow(10000):04d}"
    return f"{base}{suffix}"
