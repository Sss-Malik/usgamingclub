# Re-use the existing memorable-password generator (word + 4 digits, alphanumeric).
# Findings doc §4.7 charset rule: `[A-Za-z0-9_]`, max 32 — the GameVault generator
# emits letters+digits only (≤12 chars), satisfying both constraints with margin.
from app.backends.gamevault.passwords import generate_memorable_password


def generate_aspnet_password() -> str:
    """Memorable password for OrionStars/MilkyWay create + reset.

    Charset restricted to `[A-Za-z0-9_]` (no underscores actually emitted; the form
    allows them but the GameVault generator doesn't use them). ≤32 characters.
    """
    return generate_memorable_password()
