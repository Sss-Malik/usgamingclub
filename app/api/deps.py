# app/api/deps.py
from fastapi import HTTPException, Request

from app.config import get_settings
from app.security.hmac import verify_request


async def verify_request_signature(request: Request) -> bytes:
    raw = await request.body()
    settings = get_settings()
    ok = verify_request(
        settings.api_secret,
        request.headers.get("X-Request-Timestamp", ""),
        request.headers.get("X-Request-Signature", ""),
        raw,
        replay_window=settings.replay_window_seconds,
    )
    if not ok:
        raise HTTPException(status_code=401, detail="Signature invalid")
    return raw
