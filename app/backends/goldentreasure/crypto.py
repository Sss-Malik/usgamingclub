# app/backends/goldentreasure/crypto.py
import base64
import hashlib
import time
import urllib.parse

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

SIGN_SECRET = "#s3LEA3RpR6PNmbWtuBCPn!4gS2DNM44"


def aes_b64(plaintext: str, key: str) -> str:
    """AES-128-ECB / PKCS7 ciphertext, base64-encoded. Key must be exactly 16 ASCII chars."""
    cipher = AES.new(key.encode(), AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(pad(plaintext.encode(), 16))).decode()


def sign_body(body: dict, *, stime: int | None = None) -> tuple[str, int]:
    """Findings §3: sort body keys ascending, skip the `stime` key + empty-string/None values,
    concatenate values (raw, no separator), append str(stime) + SECRET, MD5-hex.

    Returns (sign, stime). If `stime` is not supplied, uses int(time.time()).
    """
    stime_v = stime if stime is not None else int(time.time())
    concat = "".join(
        str(body[k])
        for k in sorted(body)
        if k != "stime" and body[k] not in ("", None)
    )
    sign = hashlib.md5((concat + str(stime_v) + SIGN_SECRET).encode()).hexdigest()
    return sign, stime_v


def login_aes_key(stime: int) -> str:
    """AES-128 key for encrypting login username/password. Findings §4. MUST equal body.stime."""
    return f"123{stime}abc"


def xtoken_header(session_token: str, x_time_ms: int) -> str:
    """URL-encoded base64 of AES-128-ECB(session_token, key=f"xtu{x_time_ms}"). Findings §5.

    `session_token` is used VERBATIM — including any URL-encoded chars in the token
    string. Do not decode it before encrypting.
    """
    return urllib.parse.quote(aes_b64(session_token, f"xtu{x_time_ms}"), safe="")
