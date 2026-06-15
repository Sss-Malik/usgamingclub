from app.operations.result_cache import CachedOutcome
from app.schemas.requests import Operation
from app.webhook.payload import build_webhook_payload

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
