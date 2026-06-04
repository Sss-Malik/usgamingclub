# app/tools/ping.py
import asyncio
import json

import httpx

from app.config import Settings, get_settings
from app.security.hmac import sign


async def run_ping(settings: Settings) -> int:
    raw = json.dumps({"ping": True}, separators=(",", ":"))
    headers = sign(settings.python_signing_secret, raw)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(settings.ping_url, content=raw.encode(), headers=headers)
    print(f"ping {settings.ping_url} -> {resp.status_code} {resp.text}")
    return 0 if resp.status_code == 200 else 1


def main() -> int:
    return asyncio.run(run_ping(get_settings()))


if __name__ == "__main__":
    raise SystemExit(main())
