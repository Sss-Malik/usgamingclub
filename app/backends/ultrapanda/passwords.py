"""Re-export the memorable password generator. The vpower backend has no server-side
password complexity rule (findings §3.1) so the existing GameVault generator is plenty.
"""
from app.backends.gamevault.passwords import generate_memorable_password


def generate_vpower_password() -> str:
    """Memorable password for UltraPanda/VBlink create + reset.

    Format: `{Capitalized word}{4 digits}` (e.g. "Tiger4783"). Alphanumeric <=12 chars.
    """
    return generate_memorable_password()
