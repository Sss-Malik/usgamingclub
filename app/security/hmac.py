# app/security/hmac.py
import hashlib
import hmac
import time


def _hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def request_signature(secret: str, timestamp: str, raw_body: str | bytes) -> str:
    """Inbound scheme (Arcadia GameHttpService): HMAC over "{timestamp}.{body}", plain hex."""
    body = raw_body if isinstance(raw_body, (bytes, bytearray)) else raw_body.encode()
    return _hex(secret, f"{timestamp}.".encode() + body)


def verify_request(
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
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = now if now is not None else int(time.time())
    if abs(current - ts) > replay_window:
        return False
    return hmac.compare_digest(request_signature(secret, timestamp, raw_body), signature)


def webhook_signature(secret: str, raw_body: str | bytes) -> str:
    """Outbound scheme (Arcadia AutomationWebhookController): HMAC over the raw body, plain hex."""
    body = raw_body if isinstance(raw_body, (bytes, bytearray)) else raw_body.encode()
    return _hex(secret, body)


def sign_webhook(secret: str, raw_body: str | bytes) -> dict[str, str]:
    return {
        "X-Webhook-Signature": webhook_signature(secret, raw_body),
        "Content-Type": "application/json",
    }
