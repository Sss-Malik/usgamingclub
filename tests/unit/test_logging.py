# tests/unit/test_logging.py
from app.logging import configure_logging, get_logger, redact_processor, SECRET_KEYS


def test_redact_masks_secret_keys():
    event = {"event": "x", "backend_password": "p", "api_secret_key": "k", "balance": 10}
    out = redact_processor(None, None, event)
    assert out["backend_password"] == "***"
    assert out["api_secret_key"] == "***"
    assert out["balance"] == 10


def test_password_is_a_secret_key():
    assert "password" in SECRET_KEYS


def test_gamevault_secret_fields_are_redacted():
    # GameVault's password field is login_pwd; the per-request auth token must not leak either.
    assert {"login_pwd", "token"} <= SECRET_KEYS
    out = redact_processor(None, None, {"login_pwd": "Tiger4827", "token": "abc123"})
    assert out["login_pwd"] == "***" and out["token"] == "***"


def test_redact_is_recursive():
    event = {"event": "x", "creds": {"api_secret_key": "k", "name": "ok"}}
    out = redact_processor(None, None, event)
    assert out["creds"]["api_secret_key"] == "***"
    assert out["creds"]["name"] == "ok"


def test_goldentreasure_secret_fields_are_redacted():
    # Golden Treasure: 'pwd' is the plaintext player password in savePlayer/updatePlayer bodies;
    # 'x-token' is the per-request AES-of-session-token header. Both MUST be redacted.
    assert {"pwd", "x-token"} <= SECRET_KEYS
    out = redact_processor(None, None, {"pwd": "Tiger4827", "x-token": "jtSUNg..."})
    assert out["pwd"] == "***" and out["x-token"] == "***"


def test_aspnet_password_fields_are_redacted():
    from app.logging import _redact_in_place
    d = {
        "txtLoginPass": "secret1",
        "txtLogonPass": "secret2",
        "txtLogonPass2": "secret2",
        "txtConfirmPass": "secret3",
        "txtSureConfirmPass": "secret3",
        "ASP.NET_SessionId": "ABC123",
        "anticaptcha_api_key": "key",
        "other": "visible",
    }
    _redact_in_place(d)
    assert d["txtLoginPass"] == "***"
    assert d["txtLogonPass"] == "***"
    assert d["txtLogonPass2"] == "***"
    assert d["txtConfirmPass"] == "***"
    assert d["txtSureConfirmPass"] == "***"
    assert d["ASP.NET_SessionId"] == "***"
    assert d["anticaptcha_api_key"] == "***"
    assert d["other"] == "visible"


def test_vpower_token_and_admin_token_are_redacted():
    from app.logging import _redact_in_place
    d = {
        "admin-token": "secret1",
        "Admin-Token": "secret2",
        "auth_code": "code123",
        "x-time": "1700000000000",   # NOT secret — should NOT be redacted
        "other": "visible",
    }
    _redact_in_place(d)
    assert d["admin-token"] == "***"
    assert d["Admin-Token"] == "***"
    assert d["auth_code"] == "***"
    assert d["x-time"] == "1700000000000"
    assert d["other"] == "visible"


def test_arcadia_secret_fields_are_redacted():
    from app.logging import _redact_in_place
    d = {
        "new_password": "p",
        "amount": 50,
        "api_secret": "in",
        "webhook_secret": "out",
        "X-Webhook-Signature": "abc",
        "X-Request-Signature": "def",
    }
    _redact_in_place(d)
    assert d["new_password"] == "***"
    assert d["amount"] == "***"
    assert d["api_secret"] == "***"
    assert d["webhook_secret"] == "***"
    assert d["X-Webhook-Signature"] == "***"
    assert d["X-Request-Signature"] == "***"


def test_account_created_list_is_redacted_recursively():
    # account_created is a list of dicts in the Arcadia webhook envelope; the whole
    # field is sensitive, but we ALSO redact nested password fields in case the
    # `account_created` key is renamed in a future revision.
    from app.logging import _redact_in_place
    d = {
        "account_created": [
            {"username": "u", "password": "secretpw", "id_from_backend": "x"}
        ],
    }
    _redact_in_place(d)
    assert d["account_created"] == "***"


def test_user_data_dict_is_redacted():
    from app.logging import _redact_in_place
    d = {"user_data": {"balance": 127.5}}
    _redact_in_place(d)
    assert d["user_data"] == "***"


def test_nested_list_of_dicts_redacts_inner_secrets():
    # If a payload nests a list of dicts under a NON-secret key, nested secrets must
    # still be redacted.
    from app.logging import _redact_in_place
    d = {"events": [{"password": "p", "ok": "visible"}, {"token": "t"}]}
    _redact_in_place(d)
    assert d["events"][0]["password"] == "***"
    assert d["events"][0]["ok"] == "visible"
    assert d["events"][1]["token"] == "***"


def test_configure_logging_runs_and_logger_emits(capsys):
    # Regression: configure_logging() runs on app/worker startup. A bad structlog
    # API name here would crash the service at boot even though the redaction unit
    # tests still pass, so exercise the real configuration path.
    configure_logging()
    get_logger("test").info("hello", api_secret_key="should-be-redacted")
    out = capsys.readouterr().out
    assert "hello" in out
    assert "should-be-redacted" not in out
    assert "***" in out


def test_redacts_yolo_session_secrets():
    from app.logging import _redact_in_place
    d = {"_token": "TOK", "csrf_token": "C", "laravel_session": "S", "xsrf-token": "X", "keep": "ok"}
    _redact_in_place(d)
    assert d["_token"] == "***" and d["csrf_token"] == "***" and d["laravel_session"] == "***"
    assert d["xsrf-token"] == "***"
    assert d["keep"] == "ok"
