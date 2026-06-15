# tests/unit/test_ping.py
import httpx
import respx

from app.config import Settings
from app.tools.ping import run_ping

WEBHOOK = "https://arcadia.test/api/automation/webhook"


@respx.mock
async def test_ping_returns_0_on_200():
    settings = Settings(api_secret="in", webhook_secret="out", app_url="https://arcadia.test")
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    code = await run_ping(settings)
    assert code == 0 and route.called


@respx.mock
async def test_ping_returns_1_on_401():
    settings = Settings(api_secret="in", webhook_secret="out", app_url="https://arcadia.test")
    respx.post(WEBHOOK).mock(return_value=httpx.Response(401))
    assert await run_ping(settings) == 1
