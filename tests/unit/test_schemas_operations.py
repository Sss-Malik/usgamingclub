# tests/unit/test_schemas_operations.py
import pytest
from pydantic import ValidationError

from app.schemas.operations import operation_adapter


def test_parses_create_account():
    op = operation_adapter.validate_python(
        {"idempotency_key": "k", "type": "CREATE_ACCOUNT", "user_id": 42, "game_id": 7,
         "game_account_id": None, "account_username": "saudmalik42"}
    )
    assert op.type == "CREATE_ACCOUNT"
    assert op.game_id == 7


def test_parses_recharge_with_amounts():
    op = operation_adapter.validate_python(
        {"idempotency_key": "k", "type": "RECHARGE", "user_id": 42, "game_id": 7,
         "game_account_id": 1001, "amount_cents": 5000, "bonus_cents": 500, "total_credit_cents": 5500}
    )
    assert op.total_credit_cents == 5500


def test_parses_agent_balance_without_user():
    op = operation_adapter.validate_python(
        {"idempotency_key": "k", "type": "AGENT_BALANCE", "game_id": 7}
    )
    assert op.type == "AGENT_BALANCE"
    assert op.game_id == 7


def test_rejects_unknown_type():
    with pytest.raises(ValidationError):
        operation_adapter.validate_python({"idempotency_key": "k", "type": "NOPE", "game_id": 7})


def test_rejects_recharge_missing_amounts():
    with pytest.raises(ValidationError):
        operation_adapter.validate_python(
            {"idempotency_key": "k", "type": "RECHARGE", "user_id": 42, "game_id": 7, "game_account_id": 1001}
        )


def test_rejects_empty_idempotency_key():
    with pytest.raises(ValidationError):
        operation_adapter.validate_python(
            {"idempotency_key": "", "type": "READ_BALANCE", "user_id": 1, "game_id": 7, "game_account_id": 1}
        )


def test_create_account_requires_account_username():
    op = operation_adapter.validate_python(
        {"idempotency_key": "k", "type": "CREATE_ACCOUNT", "user_id": 42, "game_id": 9,
         "game_account_id": None, "account_username": "saudmalik42"}
    )
    assert op.account_username == "saudmalik42"


def test_create_account_without_username_is_rejected():
    with pytest.raises(ValidationError):
        operation_adapter.validate_python(
            {"idempotency_key": "k", "type": "CREATE_ACCOUNT", "user_id": 42, "game_id": 9, "game_account_id": None}
        )
