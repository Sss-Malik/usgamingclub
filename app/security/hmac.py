# app/security/hmac.py
import hashlib
import hmac
import time


def build_signature(secret: str, timestamp: str, raw_body: str | bytes) -> str:
    body = raw_body if isinstance(raw_body, (bytes, bytearray)) else raw_body.encode()
    message = f"{timestamp}.".encode() + body
    mac = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def sign(secret: str, raw_body: str | bytes, *, timestamp: int | None = None) -> dict[str, str]:
    ts = str(timestamp if timestamp is not None else int(time.time()))
    return {
        "X-Timestamp": ts,
        "X-Signature": build_signature(secret, ts, raw_body),
        "Content-Type": "application/json",
    }


def verify(
    secret: str,
    timestamp: str,
    signature: str,
    raw_body: str | bytes,
    *,
    replay_window: int = 300,
    now: int | None = None,
) -> bool:
    if not secret or not timestamp or not signature:
        return False
    # int() is stricter and safer than str.isdigit(): isdigit() accepts unicode
    # digit forms (e.g. superscripts) that then raise in int(); guard explicitly.
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = now if now is not None else int(time.time())
    if abs(current - ts) > replay_window:
        return False
    expected = build_signature(secret, timestamp, raw_body)
    return hmac.compare_digest(expected, signature)
