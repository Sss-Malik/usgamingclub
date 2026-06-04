# app/api/deps.py
from fastapi import HTTPException, Request

from app.config import get_settings
from app.security.hmac import verify


async def verify_signature(request: Request) -> bytes:
    raw = await request.body()
    settings = get_settings()
    ok = verify(
        settings.python_signing_secret,
        request.headers.get("X-Timestamp", ""),
        request.headers.get("X-Signature", ""),
        raw,  # verify over the exact raw bytes (byte-exact per contract §1)
        replay_window=settings.replay_window_seconds,
    )
    if not ok:
        raise HTTPException(status_code=401, detail="Signature invalid")
    return raw
