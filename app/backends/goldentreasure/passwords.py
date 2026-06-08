"""Golden Treasure player passwords.

Server rule (findings §8.3): 6-16 chars, must combine letters AND numbers, may include
!@#$%^/.,(). The existing GameVault memorable generator yields "Tiger4827"-style values which
satisfy the rule (alphanumeric, has letters + digits, length 9-11).
"""
from app.backends.gamevault.passwords import generate_memorable_password  # noqa: F401
