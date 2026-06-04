import httpx
import respx

from app.security.hmac import verify
from app.webhook.client import deliver_webhook

URL = "https://laravel.test/webhooks/games/operation"
SECRET = "s"
PAYLOAD = {"idempotency_key": "k", "status": "succeeded", "result": {"balance_cents": 1}}


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


async def _noop_sleep(_seconds):
    return None


@respx.mock
async def test_delivers_on_200_and_signs_request():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    async with httpx.AsyncClient() as client:
        res = await deliver_webhook(client, URL, SECRET, PAYLOAD, max_budget_seconds=600)
    assert res.delivered is True and res.status_code == 200 and res.attempts == 1
    sent = route.calls.last.request
    assert verify(
        SECRET,
        sent.headers["X-Timestamp"],
        sent.headers["X-Signature"],
        sent.content.decode(),
    )


@respx.mock
async def test_retries_on_500_then_succeeds():
    respx.post(URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json={"ok": True})]
    )
    async with httpx.AsyncClient() as client:
        res = await deliver_webhook(
            client, URL, SECRET, PAYLOAD, max_budget_seconds=600, sleep=_noop_sleep
        )
    assert res.delivered is True and res.attempts == 2


@respx.mock
async def test_does_not_retry_on_401():
    route = respx.post(URL).mock(return_value=httpx.Response(401))
    async with httpx.AsyncClient() as client:
        res = await deliver_webhook(
            client, URL, SECRET, PAYLOAD, max_budget_seconds=600, sleep=_noop_sleep
        )
    assert res.delivered is False and res.status_code == 401 and route.call_count == 1


@respx.mock
async def test_gives_up_after_budget():
    respx.post(URL).mock(return_value=httpx.Response(500))
    clock = FakeClock()

    async def advancing_sleep(seconds):
        clock.t += seconds

    async with httpx.AsyncClient() as client:
        res = await deliver_webhook(
            client, URL, SECRET, PAYLOAD,
            max_budget_seconds=5, backoff_base=1, backoff_max=4,
            now=clock, sleep=advancing_sleep,
        )
    assert res.delivered is False
