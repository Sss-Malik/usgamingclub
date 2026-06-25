# tests/integration/test_yolo_integration.py
import os

import httpx
import pytest

from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.yolo.backend import YoloBackend
from app.backends.yolo.client import YoloClient
from app.backends.yolo.session import InMemorySessionStore

_LIVE = os.getenv("YOLO_LIVE") == "1"
pytestmark = pytest.mark.skipif(not _LIVE, reason="set YOLO_LIVE=1 + creds to run")


def _ctx(account: str):
    creds = GameCredentials(
        game_id=1, name="yolo", backend_url=os.environ["YOLO_BASE_URL"], login_page_url=None,
        backend_username=os.environ["YOLO_USER"], backend_password=os.environ["YOLO_PASS"],
        api_base_url=None, api_agent_id=None, api_secret_key=None, binding_key=None,
        backend_driver="yolo",
    )
    acct = AccountIdentity(game_account_id=1, user_id=2, game_id=1,
                           username=account, external_user_id=None)
    return BackendContext(credentials=creds, user_id=2, account=acct,
                          idempotency_key="live", account_username=account)


async def test_live_agent_balance_and_read():
    async with httpx.AsyncClient(timeout=30) as http:
        client = YoloClient(
            base_url=os.environ["YOLO_BASE_URL"], username=os.environ["YOLO_USER"],
            password=os.environ["YOLO_PASS"], http_client=http,
            session_store=InMemorySessionStore(), game_id=1,
        )
        backend = YoloBackend(client)
        agent = await backend.agent_balance(_ctx(os.environ["YOLO_TEST_ACCOUNT"]))
        assert agent.agent_balance >= 0
        bal = await backend.read_balance(_ctx(os.environ["YOLO_TEST_ACCOUNT"]))
        assert bal.balance >= 0
