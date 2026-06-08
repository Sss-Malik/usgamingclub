import urllib.parse

from app.backends.goldentreasure.crypto import (
    SIGN_SECRET,
    aes_b64,
    login_aes_key,
    sign_body,
    xtoken_header,
)


# Oracles below come from the findings doc §3, §4, §5 — every value verified against the live API.

def test_secret_matches_findings():
    assert SIGN_SECRET == "#s3LEA3RpR6PNmbWtuBCPn!4gS2DNM44"


def test_login_aes_key_is_123_stime_abc():
    assert login_aes_key(1779281935) == "1231779281935abc"
    assert len(login_aes_key(1779281935)) == 16     # AES-128 key


def test_aes_b64_login_username_oracle():
    # findings §4
    assert aes_b64("Test02Gd1WEB", "1231779281935abc") == "BXrmQgZgqwThh5+CjFOLFA=="


def test_aes_b64_login_password_oracle():
    # findings §4
    assert aes_b64("Zaeem@1233", "1231779281935abc") == "suyUHuDw+rXOKpJvvW7WsA=="


def test_sign_body_empty_oracle():
    # findings §3 — getLoginNote: empty body, stime=1779281921
    sign, stime = sign_body({}, stime=1779281921)
    assert sign == "1f8aca4093e5002f7481e9d7266b9ceb"
    assert stime == 1779281921


def test_sign_body_save_player_oracle_verifies_empty_skip_and_sort():
    # findings §3 — savePlayer body with empty fields (name/phone/tel_area_code/remark)
    # MUST be skipped during concatenation; keys sorted ascending.
    body = {
        "token": "q5pIWNNzvi%2BpBHDQYDLPnFnckAzxSNbIcEVrTxn%2F%2FTg%3D",
        "account": "apitest01",
        "pwd": "Apitest123",
        "score": "0",
        "name": "",                     # skipped
        "phone": "",                    # skipped
        "tel_area_code": "",            # skipped
        "remark": "",                   # skipped
    }
    sign, _ = sign_body(body, stime=1779282067)
    assert sign == "2fb7d0fb23cce1d967f095352b5bfa3f"


def test_sign_body_skips_none_and_stime_key_itself():
    # `stime` key in the body must be skipped during concat (it's appended as a suffix).
    # `None` values must also be skipped.
    sign, _ = sign_body({"a": "x", "b": None, "stime": 1234567890}, stime=1234567890)
    # Manual: concat = "x"; sign = MD5("x" + "1234567890" + SECRET)
    import hashlib
    expected = hashlib.md5(("x" + "1234567890" + SIGN_SECRET).encode()).hexdigest()
    assert sign == expected


def test_sign_body_defaults_stime_to_now_when_missing(monkeypatch):
    import app.backends.goldentreasure.crypto as crypto_module
    monkeypatch.setattr(crypto_module.time, "time", lambda: 1234567890.5)
    sign, stime = sign_body({})
    assert stime == 1234567890                       # int(time.time())


def test_xtoken_header_oracle():
    # findings §5
    session_token = "q5pIWNNzvi%2BpBHDQYDLPnFnckAzxSNbIcEVrTxn%2F%2FTg%3D"
    expected_b64 = (
        "jtSUNgHpXUUdEO+0ksqlndADWqFtaseFwSYCvXZq7l0dwKMicOPagiYFe84+hU6xbU4Xw6kmPKJfwGigrquoJg=="
    )
    expected_url = urllib.parse.quote(expected_b64, safe="")
    assert xtoken_header(session_token, 1779281936505) == expected_url
