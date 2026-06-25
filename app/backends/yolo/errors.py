from app.backends.base import BackendError, TransientBackendError

# Substring (case-insensitive) -> terminal reason slug. Business (200+status:false) + validation (422).
_PATTERNS: list[tuple[str, str]] = [
    ("score is insufficient", "insufficient_balance"),
    ("already been taken", "account_exists"),
    ("format is invalid", "account_invalid"),
    ("at least 6 characters", "too_short"),
    ("required", "field_required"),
]


def _slug(message: str) -> str | None:
    low = (message or "").lower()
    for needle, slug in _PATTERNS:
        if needle in low:
            return slug
    return None


def map_envelope(http_status: int, body: dict | None) -> dict:
    """Classify a YOLO response. Returns the success `data` dict, or raises.

    Three envelopes (findings §7): 200+status:true success; 200+status:false business error
    (`data.message`); 422 validation error (`errors{}`). 5xx / non-JSON -> transient.
    """
    if http_status >= 500:
        raise TransientBackendError(f"yolo:http_{http_status}")
    if body is None:
        raise TransientBackendError("yolo:bad_response")

    if http_status == 422 or "errors" in body:
        errors = body.get("errors") or {}
        field, msgs = next(iter(errors.items()), ("", [""]))
        msg = msgs[0] if isinstance(msgs, list) and msgs else ""
        slug = _slug(msg)
        if slug:
            raise BackendError(f"yolo:{slug}")
        raise BackendError(f"yolo:validation_error: {field}: {msg[:60]}")

    if body.get("status") is True:
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    # status:false business error
    data = body.get("data")
    msg = data.get("message", "") if isinstance(data, dict) else ""
    slug = _slug(msg)
    if slug:
        raise BackendError(f"yolo:{slug}")
    raise BackendError(f"yolo:business_error: {msg[:80]}")


def looks_like_auth_failure(status_code: int, location: str, text: str) -> bool:
    """True when a response indicates the admin session/CSRF is no longer valid."""
    if status_code in (401, 419):
        return True
    if status_code in (301, 302) and "/admin/auth/login" in (location or ""):
        return True
    # Lazy import to avoid a parsers<->errors cycle at module load.
    from app.backends.yolo.parsers import looks_like_login_page
    return looks_like_login_page(text or "")
