# tests/integration/test_executor.py
import httpx
import respx

from app.config import Settings
from app.operations.executor import execute_operation

URL = "https://laravel.test/webhooks/games/operation"


def _settings():
    return Settings(
        python_signing_secret="s",
        app_url="https://laravel.test",
        webhook_max_budget_seconds=600,
    )


async def _run(payload, session_factory):
    async with httpx.AsyncClient() as client:
        await execute_operation(
            payload,
            session_factory=session_factory,
            http_client=client,
            settings=_settings(),
        )


@respx.mock
async def test_read_balance_success_posts_succeeded_webhook(seeded):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await _run(
        {"idempotency_key": "k1", "type": "READ_BALANCE", "user_id": 42, "game_id": 7, "game_account_id": 1001},
        seeded,
    )
    body = route.calls.last.request.content.decode()
    assert '"status":"succeeded"' in body
    assert '"balance_cents":12750' in body
    assert '"idempotency_key":"k1"' in body


@respx.mock
async def test_create_account_includes_username_password(seeded):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await _run(
        {"idempotency_key": "k2", "type": "CREATE_ACCOUNT", "user_id": 42, "game_id": 7, "game_account_id": None},
        seeded,
    )
    body = route.calls.last.request.content.decode()
    assert '"username":"mock_42_7"' in body and '"password":"' in body


@respx.mock
async def test_preflight_failure_posts_failed_webhook(seeded):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await _run(
        {"idempotency_key": "k3", "type": "REDEEM", "user_id": 42, "game_id": 7, "game_account_id": 999, "amount_cents": 100},
        seeded,
    )
    body = route.calls.last.request.content.decode()
    assert '"status":"failed"' in body and "game_account_not_found" in body


@respx.mock
async def test_invalid_payload_posts_failed_webhook(seeded):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await _run({"idempotency_key": "k4", "type": "NOPE"}, seeded)
    body = route.calls.last.request.content.decode()
    assert '"status":"failed"' in body and "invalid_payload" in body
