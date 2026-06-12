"""Three crypto primitives for the UltraPanda/VBlink vpower backend.

All three are reverse-engineered from the vendor's JS bundle and verified byte-for-byte
against captured traffic; see /Applications/development/ultrapanda-standalone/api_findings.md §1.
"""
import base64
import hashlib
from urllib.parse import quote, unquote

from Crypto.Cipher import AES


SIGN_SECRET = "#s3LEA3RpR6PNmbWtuBCPn!4gS2DNM44"
"""Hardcoded signing secret extracted from the vendor's JS bundle (findings §1.2)."""


def _pkcs7_pad(b: bytes) -> bytes:
    pad = 16 - (len(b) % 16)
    return b + bytes([pad]) * pad


def _pkcs7_unpad(b: bytes) -> bytes:
    pad = b[-1]
    return b[:-pad]


def _aes_ecb_encrypt_b64(plaintext: str, key: str) -> str:
    ct = AES.new(key.encode(), AES.MODE_ECB).encrypt(_pkcs7_pad(plaintext.encode()))
    return base64.b64encode(ct).decode()


def _aes_ecb_decrypt_b64(b64: str, key: str) -> str:
    pt = AES.new(key.encode(), AES.MODE_ECB).decrypt(base64.b64decode(b64))
    return _pkcs7_unpad(pt).decode()


def encrypt_login_cred(plain: str, stime_sec: int) -> str:
    """AES-128-ECB + PKCS7 + base64 with key = '123' + str(stime_sec) + 'abc'.

    Used for `username`/`password` on POST /user/login. Key length: 3+10+3 = 16 bytes.
    """
    return _aes_ecb_encrypt_b64(plain, "123" + str(stime_sec) + "abc")


def sign_body(body: dict, stime_sec: int) -> str:
    """MD5( ''.join(str(v) for k,v in sorted(body) if k!='stime' and v not in ('', None))
           + str(stime_sec) + SIGN_SECRET ). Returns lowercase hex."""
    concat = ""
    for k in sorted(body):
        if k == "stime":
            continue
        v = body[k]
        if v == "" or v is None:
            continue
        concat += str(v)
    return hashlib.md5((concat + str(stime_sec) + SIGN_SECRET).encode()).hexdigest()


def encrypt_xtoken(admin_token: str, ms_time: int) -> str:
    """AES-128-ECB+PKCS7 of `admin_token` (URL-encoded form, verbatim),
    key = 'xtu' + str(ms_time). Returns urlencode(base64(ciphertext))."""
    b64 = _aes_ecb_encrypt_b64(admin_token, "xtu" + str(ms_time))
    return quote(b64, safe="")


def decrypt_xtoken(xtoken_value: str, ms_time: int) -> str:
    """Inverse of encrypt_xtoken; used only by tests for round-trip verification."""
    b64 = unquote(xtoken_value)
    return _aes_ecb_decrypt_b64(b64, "xtu" + str(ms_time))
