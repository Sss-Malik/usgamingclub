# tests/unit/test_backend_base.py
from app.backends.base import BackendError, TransientBackendError
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


def test_backend_error_carries_optional_provider_fields():
    err = BackendError("gamevault:7:insufficient_user_balance",
                       provider_http_status=200, provider_code=7,
                       provider_message="user balance not enough")
    assert err.reason == "gamevault:7:insufficient_user_balance"
    assert err.provider_http_status == 200
    assert err.provider_code == 7
    assert err.provider_message == "user balance not enough"


def test_backend_error_provider_fields_default_none():
    err = BackendError("boom")
    assert err.provider_http_status is None
    assert err.provider_code is None
    assert err.provider_message is None


def test_transient_error_carries_provider_fields():
    err = TransientBackendError("gamevault_http_503", provider_http_status=503)
    assert isinstance(err, BackendError)
    assert err.provider_http_status == 503
    assert str(err) == "gamevault_http_503"
