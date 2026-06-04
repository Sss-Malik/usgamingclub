# tests/unit/test_hmac.py
import hashlib
import hmac as _hmac

from app.security.hmac import build_signature, sign, verify

SECRET = "shared-secret"
BODY = '{"idempotency_key":"abc","type":"READ_BALANCE"}'


def test_build_signature_matches_reference_algorithm():
    ts = "1733345678"
    expected = "sha256=" + _hmac.new(
        SECRET.encode(), f"{ts}.{BODY}".encode(), hashlib.sha256
    ).hexdigest()
    assert build_signature(SECRET, ts, BODY) == expected


def test_sign_then_verify_roundtrip():
    headers = sign(SECRET, BODY, timestamp=1000)
    assert headers["Content-Type"] == "application/json"
    assert verify(SECRET, headers["X-Timestamp"], headers["X-Signature"], BODY, now=1000)


def test_verify_rejects_tampered_body():
    headers = sign(SECRET, BODY, timestamp=1000)
    assert not verify(SECRET, headers["X-Timestamp"], headers["X-Signature"], BODY + "x", now=1000)


def test_verify_rejects_expired_timestamp():
    headers = sign(SECRET, BODY, timestamp=1000)
    assert not verify(SECRET, headers["X-Timestamp"], headers["X-Signature"], BODY, now=1000 + 301)


def test_verify_rejects_non_numeric_or_missing():
    assert not verify(SECRET, "not-a-number", "sha256=x", BODY, now=1000)
    assert not verify(SECRET, "1000", "", BODY, now=1000)
    assert not verify("", "1000", "sha256=x", BODY, now=1000)


def test_verify_rejects_unicode_digit_timestamp():
    # "³" (superscript 3) passes str.isdigit() but is not int-parseable; must be rejected.
    assert not verify(SECRET, "³", "sha256=x", BODY, now=1000)


def test_sign_and_verify_over_raw_bytes_match_str_path():
    body_bytes = BODY.encode()
    headers = sign(SECRET, body_bytes, timestamp=1000)
    # signature over bytes equals signature over the equivalent str
    assert headers["X-Signature"] == build_signature(SECRET, "1000", BODY)
    assert verify(SECRET, headers["X-Timestamp"], headers["X-Signature"], body_bytes, now=1000)
    assert not verify(SECRET, headers["X-Timestamp"], headers["X-Signature"], body_bytes + b"x", now=1000)
