import json

import httpx
import respx

from app.security.hmac import webhook_signature
from app.webhook.client import deliver_webhook

URL = "https://arcadia.test/api/automation/webhook"
SECRET = "out-secret"
PAYLOAD = {"action": "recharge", "status": "success", "user_id": 1}


class FakeClock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


async def _noop_sleep(_s): return None


@respx.mock
async def test_delivers_on_200_signs_raw_body_with_fresh_timestamp():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"success": True}))
    async with httpx.AsyncClient() as client:
        res = await deliver_webhook(client, URL, SECRET, PAYLOAD,
                                    max_budget_seconds=600, now_unix=lambda: 1234)
    assert res.delivered and res.status_code == 200 and res.attempts == 1
    sent = route.calls.last.request
    body = sent.content.decode()
    assert json.loads(body)["timestamp"] == 1234
    assert sent.headers["X-Webhook-Signature"] == webhook_signature(SECRET, body)


@respx.mock
async def test_retries_on_500_then_succeeds_and_refreshes_timestamp():
    times = iter([100, 200])
    respx.post(URL).mock(side_effect=[httpx.Response(500), httpx.Response(200)])
    async with httpx.AsyncClient() as client:
        res = await deliver_webhook(client, URL, SECRET, PAYLOAD, max_budget_seconds=600,
                                    sleep=_noop_sleep, now_unix=lambda: next(times))
    assert res.delivered and res.attempts == 2


@respx.mock
async def test_does_not_retry_on_403():
    route = respx.post(URL).mock(return_value=httpx.Response(403))
    async with httpx.AsyncClient() as client:
        res = await deliver_webhook(client, URL, SECRET, PAYLOAD, max_budget_seconds=600,
                                    sleep=_noop_sleep)
    assert not res.delivered and res.status_code == 403 and route.call_count == 1


@respx.mock
async def test_gives_up_after_budget():
    respx.post(URL).mock(return_value=httpx.Response(500))
    clock = FakeClock()

    async def advancing_sleep(s): clock.t += s

    async with httpx.AsyncClient() as client:
        res = await deliver_webhook(client, URL, SECRET, PAYLOAD, max_budget_seconds=5,
                                    backoff_base=1, backoff_max=4, now=clock, sleep=advancing_sleep)
    assert not res.delivered
