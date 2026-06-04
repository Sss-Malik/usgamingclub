# tests/unit/test_ping.py
import httpx
import respx

from app.config import Settings
from app.tools.ping import run_ping


@respx.mock
async def test_ping_returns_0_on_200():
    settings = Settings(python_signing_secret="s", app_url="https://laravel.test")
    route = respx.post("https://laravel.test/webhooks/_ping").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    code = await run_ping(settings)
    assert code == 0 and route.called


@respx.mock
async def test_ping_returns_1_on_401():
    settings = Settings(python_signing_secret="s", app_url="https://laravel.test")
    respx.post("https://laravel.test/webhooks/_ping").mock(return_value=httpx.Response(401))
    assert await run_ping(settings) == 1
