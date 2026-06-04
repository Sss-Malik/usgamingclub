import asyncio
import json
import random
import time
from dataclasses import dataclass

import httpx

from app.logging import get_logger
from app.security.hmac import sign

logger = get_logger(__name__)

NO_RETRY_STATUSES = {401, 422}


@dataclass
class WebhookResult:
    delivered: bool
    status_code: int | None
    attempts: int


async def deliver_webhook(
    client: httpx.AsyncClient,
    url: str,
    secret: str,
    payload: dict,
    *,
    max_budget_seconds: float,
    backoff_base: float = 0.5,
    backoff_max: float = 30.0,
    now=time.monotonic,
    sleep=asyncio.sleep,
) -> WebhookResult:
    raw = json.dumps(payload, separators=(",", ":"))
    deadline = now() + max_budget_seconds
    attempt = 0
    last_status: int | None = None

    while True:
        attempt += 1
        # Re-sign every attempt: the 300s replay window means a stale timestamp
        # would be rejected once retries span more than five minutes.
        headers = sign(secret, raw)
        try:
            resp = await client.post(url, content=raw.encode(), headers=headers)
            last_status = resp.status_code
            if last_status == 200:
                logger.info("webhook_delivered", phase="webhook_delivered", attempts=attempt)
                return WebhookResult(True, 200, attempt)
            if last_status in NO_RETRY_STATUSES:
                logger.error(
                    "webhook_sender_bug", phase="webhook_attempt", status=last_status
                )
                return WebhookResult(False, last_status, attempt)
        except httpx.HTTPError as exc:
            last_status = None
            logger.warning("webhook_conn_error", phase="webhook_attempt", error=str(exc))

        delay = min(backoff_max, backoff_base * (2 ** (attempt - 1)))
        delay += random.uniform(0, delay * 0.25)
        if now() + delay >= deadline:
            logger.error("webhook_gave_up", phase="failed", attempts=attempt, status=last_status)
            return WebhookResult(False, last_status, attempt)
        await sleep(delay)
