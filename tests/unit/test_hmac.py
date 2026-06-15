import hashlib
import hmac as _hmac

from app.security.hmac import (
    request_signature,
    sign_webhook,
    verify_request,
    webhook_signature,
)

SECRET = "shared-secret"
BODY = '{"user_id":1,"backend_name":"milkyway"}'


def test_request_signature_matches_php_reference():
    ts = "1733345678"
    expected = _hmac.new(SECRET.encode(), f"{ts}.{BODY}".encode(), hashlib.sha256).hexdigest()
    assert request_signature(SECRET, ts, BODY) == expected  # plain hex, no prefix


def test_verify_request_roundtrip():
    ts = "1000"
    sig = request_signature(SECRET, ts, BODY)
    assert verify_request(SECRET, ts, sig, BODY, now=1000)


def test_verify_request_rejects_tamper_and_stale_and_missing():
    ts = "1000"
    sig = request_signature(SECRET, ts, BODY)
    assert not verify_request(SECRET, ts, sig, BODY + "x", now=1000)
    assert not verify_request(SECRET, ts, sig, BODY, now=1000 + 301)
    assert not verify_request(SECRET, "nan", sig, BODY, now=1000)
    assert not verify_request(SECRET, ts, "", BODY, now=1000)
    assert not verify_request("", ts, sig, BODY, now=1000)


def test_webhook_signature_matches_php_reference():
    expected = _hmac.new(SECRET.encode(), BODY.encode(), hashlib.sha256).hexdigest()
    assert webhook_signature(SECRET, BODY) == expected
    headers = sign_webhook(SECRET, BODY)
    assert headers["X-Webhook-Signature"] == expected
    assert headers["Content-Type"] == "application/json"


def test_signatures_work_over_bytes():
    assert webhook_signature(SECRET, BODY.encode()) == webhook_signature(SECRET, BODY)
