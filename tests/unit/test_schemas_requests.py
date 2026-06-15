import pytest
from pydantic import ValidationError

from app.schemas.requests import (
    CreateRequest,
    FreeplayRequest,
    ReadRequest,
    RechargeRequest,
    ResetPasswordRequest,
    WithdrawRequest,
    Operation,
)


def test_recharge_request_valid():
    r = RechargeRequest.model_validate({
        "user_id": 1, "backend_name": "milkyway", "username": "p1",
        "amount": 50, "transaction_id": "uuid-1",
    })
    assert r.amount == 50 and r.transaction_id == "uuid-1"


def test_recharge_request_rejects_missing_fields():
    with pytest.raises(ValidationError):
        RechargeRequest.model_validate({"user_id": 1, "backend_name": "x"})


def test_create_request_needs_full_name():
    r = CreateRequest.model_validate({"user_id": 1, "full_name": "John Doe", "backend_name": "mw"})
    assert r.full_name == "John Doe"


def test_operation_roundtrips_via_dict():
    op = Operation.model_validate({
        "action": "recharge", "type": "RECHARGE", "idempotency_key": "recharge:uuid-1",
        "user_id": 1, "backend_name": "milkyway", "username": "p1", "amount": 50,
        "correlation": {"transaction_id": "uuid-1"},
    })
    assert op.action == "recharge" and op.correlation["transaction_id"] == "uuid-1"
