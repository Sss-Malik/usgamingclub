from app.operations.result_cache import CachedOutcome
from app.schemas.requests import Operation
from app.webhook.payload import assemble_diagnostics, build_webhook_payload

GENERIC = "Something went wrong. Please try again later."


def _op(action, type_, **kw):
    base = dict(action=action, type=type_, idempotency_key="k", user_id=42,
                backend_name="milkyway")
    base.update(kw)
    return Operation(**base)


def test_recharge_success_echoes_amount_and_txn():
    op = _op("recharge", "RECHARGE", amount=50, correlation={"transaction_id": "uuid-1"})
    out = CachedOutcome("succeeded", {"balance": 1234.0}, None)
    body = build_webhook_payload(op, out, backend_id=1)
    assert body["action"] == "recharge" and body["status"] == "success"
    assert body["user_id"] == 42 and body["backend_id"] == 1 and body["backend_name"] == "milkyway"
    assert body["transaction_id"] == "uuid-1" and body["amount"] == 50
    assert isinstance(body["timestamp"], int)


def test_recharge_failure_keeps_txn_and_amount_with_message():
    op = _op("recharge", "RECHARGE", amount=50, correlation={"transaction_id": "uuid-1"})
    out = CachedOutcome("failed", None, "backend_error: Insufficient balance")
    body = build_webhook_payload(op, out, backend_id=1)
    assert body["status"] == "failed" and body["transaction_id"] == "uuid-1"
    assert body["amount"] == 50 and body["message"] == "Insufficient balance"


def test_error_status_generic_message():
    op = _op("recharge", "RECHARGE", amount=50, correlation={"transaction_id": "uuid-1"})
    out = CachedOutcome("error", None, "backend_error: unexpected")
    body = build_webhook_payload(op, out, backend_id=1)
    assert body["status"] == "error" and body["message"] == GENERIC


def test_create_success_account_created():
    op = _op("create", "CREATE_ACCOUNT", account_username="janedoe1234")
    out = CachedOutcome("succeeded",
                        {"username": "janedoe1234", "password": "p", "external_user_id": "u:g"}, None)
    body = build_webhook_payload(op, out, backend_id=1)
    assert body["account_created"] == [
        {"username": "janedoe1234", "password": "p", "id_from_backend": "u:g"}
    ]


def test_reset_password_success_new_password():
    op = _op("reset_password", "RESET_PASSWORD", correlation={"reset_password_id": 9})
    out = CachedOutcome("succeeded", {"password": "newpw"}, None)
    body = build_webhook_payload(op, out, backend_id=1)
    assert body["reset_password_id"] == 9 and body["new_password"] == "newpw"


def test_read_success_balance_dollars():
    op = _op("read", "READ_BALANCE", correlation={"read_id": 5})
    out = CachedOutcome("succeeded", {"balance": 127.5}, None)
    body = build_webhook_payload(op, out, backend_id=1)
    assert body["read_id"] == 5 and body["user_data"] == {"balance": 127.5}


def test_assemble_omits_untruthful_fields():
    d = assemble_diagnostics(op_id=None, idempotency_key="read:1", attempt=1,
                             cache_hit=False, duration_ms=12)
    assert d == {"idempotency_key": "read:1", "attempt": 1, "cache_hit": False,
                 "duration_ms": 12, "steps": []}
    assert "op_id" not in d and "session_reuse" not in d and "provider" not in d


def test_assemble_includes_failure_and_provider():
    snap = {"steps": [{"name": "recharge.post", "phase": "primary", "http": True,
                       "external": False, "ok": False, "ms": 800}],
            "session_reuse": "hit", "external_user_id": None,
            "balance_before": None, "balance_after": None}
    d = assemble_diagnostics(op_id="01J", idempotency_key="recharge:1", attempt=1,
                             cache_hit=False, duration_ms=900, snapshot=snap,
                             failure_kind="backend",
                             reason="gamevault:7:insufficient_user_balance",
                             provider={"http_status": 200, "code": "7", "message": "no funds"})
    assert d["op_id"] == "01J"
    assert d["session_reuse"] == "hit"
    assert d["failure_kind"] == "backend"
    assert d["reason"] == "gamevault:7:insufficient_user_balance"
    assert d["provider"] == {"http_status": 200, "code": "7", "message": "no funds"}
    assert d["steps"][0]["name"] == "recharge.post"
    assert "external_user_id" not in d  # None → omitted


def test_assemble_drops_empty_provider():
    d = assemble_diagnostics(op_id=None, idempotency_key="k", attempt=1, cache_hit=False,
                             duration_ms=1, failure_kind="transient", reason="gamevault_http_503",
                             provider={"http_status": None, "code": None, "message": None})
    assert "provider" not in d


def test_build_payload_without_diagnostics_is_legacy_shape():
    op = _op("read", "READ_BALANCE", correlation={"read_id": 5})
    out = CachedOutcome("succeeded", {"balance": 127.5}, None)
    body = build_webhook_payload(op, out, backend_id=1)
    assert "diagnostics" not in body and "op_id" not in body


def test_build_payload_attaches_diagnostics_and_top_level_op_id():
    op = _op("read", "READ_BALANCE", correlation={"read_id": 5}, op_id="01J")
    out = CachedOutcome("succeeded", {"balance": 127.5}, None)
    diag = {"idempotency_key": "read:1", "attempt": 1, "cache_hit": False,
            "duration_ms": 3, "steps": [], "op_id": "01J"}
    body = build_webhook_payload(op, out, backend_id=1, diagnostics=diag)
    assert body["op_id"] == "01J"
    assert body["diagnostics"] is diag
    assert body["message"] == ""  # unchanged


def test_error_message_still_generic_but_reason_can_differ():
    op = _op("recharge", "RECHARGE", amount=5, correlation={"transaction_id": "t"})
    out = CachedOutcome("error", None, "backend_error: gamevault_http_503")
    diag = {"idempotency_key": "recharge:t", "attempt": 1, "cache_hit": False,
            "duration_ms": 5, "steps": [], "failure_kind": "transient",
            "reason": "gamevault_http_503"}
    body = build_webhook_payload(op, out, backend_id=1, diagnostics=diag)
    assert body["message"] == GENERIC
    assert body["diagnostics"]["reason"] == "gamevault_http_503"
