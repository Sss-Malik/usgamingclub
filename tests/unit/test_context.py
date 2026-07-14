from app.backends.context import BackendContext, GameCredentials
from app.backends.diagnostics import NULL_RECORDER, DiagnosticsRecorder


def _creds():
    return GameCredentials(game_id=1, name="g", backend_url=None, login_page_url=None,
                           backend_username=None, backend_password=None, api_base_url=None,
                           api_agent_id=None, api_secret_key=None, binding_key=None)


def test_context_defaults_diag_to_null_recorder():
    ctx = BackendContext(credentials=_creds(), user_id=1, account=None)
    assert ctx.diagnostics is None
    assert ctx.diag is NULL_RECORDER
    assert ctx.op_id is None
    assert ctx.attempt == 1


def test_context_diag_returns_supplied_recorder():
    rec = DiagnosticsRecorder()
    ctx = BackendContext(credentials=_creds(), user_id=1, account=None,
                         diagnostics=rec, op_id="01J", attempt=2)
    assert ctx.diag is rec
    assert ctx.op_id == "01J"
    assert ctx.attempt == 2
