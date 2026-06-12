import re
from urllib.parse import unquote

from app.backends.ultrapanda.crypto import (
    SIGN_SECRET,
    decrypt_xtoken,
    encrypt_login_cred,
    encrypt_xtoken,
    sign_body,
)


# --- encrypt_login_cred ---

def test_encrypt_login_cred_username_fixture():
    """Fixture from findings doc §1.1, verified against captured traffic."""
    assert encrypt_login_cred("TestUP159", 1781187351) == "VhMfl38nq02TCY8sqZu5mg=="


def test_encrypt_login_cred_password_fixture():
    """Fixture from findings doc §1.1."""
    assert encrypt_login_cred("Test1234", 1781187351) == "j9HtJjroTJboYOiA/nGdlQ=="


def test_encrypt_login_cred_uses_different_keys_per_timestamp():
    a = encrypt_login_cred("hello", 1700000000)
    b = encrypt_login_cred("hello", 1700000001)
    assert a != b


# --- sign_body ---

def test_sign_secret_matches_findings_doc():
    assert SIGN_SECRET == "#s3LEA3RpR6PNmbWtuBCPn!4gS2DNM44"


def test_sign_body_login_fixture_matches_captured_md5():
    """The captured login body from findings §3 (Login) produced
    sign = "fb9013238f78c92d7713fe5523e8b16a" (findings §1.2)."""
    body = {
        "username": "VhMfl38nq02TCY8sqZu5mg==",
        "password": "j9HtJjroTJboYOiA/nGdlQ==",
        "stime": 1781187351,
        "auth_code": "",
    }
    assert sign_body(body, 1781187351) == "fb9013238f78c92d7713fe5523e8b16a"


def test_sign_body_skips_empty_and_null_values():
    a = sign_body({"a": "x", "b": "", "c": None, "d": "y"}, 1234567890)
    b = sign_body({"a": "x", "d": "y"}, 1234567890)
    assert a == b


def test_sign_body_skips_stime_key_from_concat():
    a = sign_body({"a": "x", "stime": 1234567890}, 1234567890)
    b = sign_body({"a": "x"}, 1234567890)
    assert a == b


def test_sign_body_sorts_keys_alphabetically_before_concat():
    a = sign_body({"z": "1", "a": "2", "m": "3"}, 1234567890)
    b = sign_body({"a": "2", "m": "3", "z": "1"}, 1234567890)
    assert a == b


def test_sign_body_returns_lowercase_hex():
    s = sign_body({"a": "x"}, 1234567890)
    assert re.fullmatch(r"[0-9a-f]{32}", s)


# --- encrypt_xtoken ---

def test_encrypt_xtoken_round_trip_with_known_token_and_key():
    """Doc gives the input (URL-encoded token + ms_time) but not the exact ciphertext.
    Verify via round-trip: decrypt(encrypt(t, key), key) == t."""
    token = "Ul%2Ba9iVUvWnqlti2VP%2BatFnckAzxSNbIcEVrTxn%2F%2FTg%3D"
    out = encrypt_xtoken(token, 1781187352387)
    assert isinstance(out, str)
    assert "%" in out  # URL-encoded
    assert decrypt_xtoken(out, 1781187352387) == token


def test_encrypt_xtoken_preserves_token_url_encoded_form():
    """Token used VERBATIM as plaintext — no decoding before encryption.
    Findings §1.3: JS stores sessionStorage['Admin-Token'] URL-encoded and uses
    that exact stored string as plaintext for x-token AES."""
    token_urlenc = "abc%2Fdef%3D"
    out = encrypt_xtoken(token_urlenc, 1781187352387)
    assert decrypt_xtoken(out, 1781187352387) == token_urlenc


def test_encrypt_xtoken_uses_xtu_plus_ms_as_key_length_16():
    """ms_time is 13 digits; key = 'xtu' + 13 digits = 16 bytes → AES-128."""
    out = encrypt_xtoken("plaintext_token_value", 1234567890123)
    decoded_once = unquote(out)
    import base64
    decoded_bytes = base64.b64decode(decoded_once)
    assert len(decoded_bytes) % 16 == 0
