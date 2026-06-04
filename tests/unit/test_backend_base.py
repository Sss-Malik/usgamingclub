# tests/unit/test_backend_base.py
from app.backends.base import BackendError
from app.backends.context import AccountIdentity, BackendContext, GameCredentials


def test_backend_error_carries_reason():
    err = BackendError("game backend timeout")
    assert err.reason == "game backend timeout"
    assert str(err) == "game backend timeout"


def test_context_dataclasses_construct():
    creds = GameCredentials(
        game_id=7, name="Demo", backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url="x", api_agent_id="a", api_secret_key="s", binding_key="b",
    )
    acct = AccountIdentity(game_account_id=1001, user_id=42, game_id=7, username="u", external_user_id="E")
    ctx = BackendContext(credentials=creds, user_id=42, account=acct)
    assert ctx.credentials.game_id == 7
    assert ctx.account.username == "u"
