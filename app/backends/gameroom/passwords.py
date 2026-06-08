import secrets

# Re-export the existing memorable (alphanumeric) generator: Gameroom CREATE_ACCOUNT rule is
# alphanumeric 6-12 chars (same as GameVault), so the existing generator already satisfies it.
from app.backends.gamevault.passwords import generate_memorable_password  # noqa: F401

# Filter the existing wordlist to 4-7 char words so word+symbol+2 digits stays within 6-12.
from app.backends.gamevault.passwords import _WORDS as _GV_WORDS

_SHORT_WORDS: tuple[str, ...] = tuple(w for w in _GV_WORDS if 4 <= len(w) <= 7)
_SYMBOLS = "!@#$%&*"  # safe set: no quote/space/paren


def generate_memorable_complex_password() -> str:
    """Memorable password satisfying Gameroom's RESET rule:
    upper + lower + special symbol + 6-12 chars. Format: {Word}{symbol}{2 digits}, e.g. 'Tiger@47'.
    """
    word = secrets.choice(_SHORT_WORDS)
    symbol = secrets.choice(_SYMBOLS)
    number = secrets.randbelow(90) + 10  # 10..99
    return f"{word}{symbol}{number}"
