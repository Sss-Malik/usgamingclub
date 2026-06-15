# app/tools/ping.py
import asyncio
import json
import time

import httpx

from app.config import Settings, get_settings
from app.security.hmac import sign_webhook


async def run_ping(settings: Settings) -> int:
    """Connectivity/auth smoke check against the Arcadia webhook endpoint.

    Arcadia has no dedicated ping route, so we send a signed (webhook_secret) probe to
    `webhook_url`. A 200 means the endpoint accepted the signed body; any other status is
    surfaced so an operator can tell "reachable but rejected" from "unreachable".
    """
    raw = json.dumps({"ping": True, "timestamp": int(time.time())}, separators=(",", ":"))
    headers = sign_webhook(settings.webhook_secret, raw)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(settings.webhook_url, content=raw.encode(), headers=headers)
    print(f"ping {settings.webhook_url} -> {resp.status_code} {resp.text}")
    return 0 if resp.status_code == 200 else 1


def main() -> int:
    return asyncio.run(run_ping(get_settings()))


if __name__ == "__main__":
    raise SystemExit(main())
