# tests/unit/test_schemas_results.py
import pytest
from pydantic import ValidationError

from app.schemas.results import (
    AgentBalanceResult,
    CreateAccountResult,
    ReadBalanceResult,
    RechargeResult,
    ResetPasswordResult,
)


def test_create_account_dump_omits_none_external_id():
    r = CreateAccountResult(username="u", password="p")
    assert r.model_dump(exclude_none=True) == {"username": "u", "password": "p"}


def test_create_account_rejects_empty_external_id():
    with pytest.raises(ValidationError):
        CreateAccountResult(username="u", password="p", external_user_id="")


def test_read_balance_requires_non_negative_int():
    assert ReadBalanceResult(balance_cents=0).balance_cents == 0
    with pytest.raises(ValidationError):
        ReadBalanceResult(balance_cents=-1)


def test_recharge_balance_optional_and_omitted_when_none():
    assert RechargeResult().model_dump(exclude_none=True) == {}


def test_agent_balance_required():
    assert AgentBalanceResult(agent_balance_cents=100).agent_balance_cents == 100
    with pytest.raises(ValidationError):
        AgentBalanceResult()


def test_reset_password_required():
    assert ResetPasswordResult(password="x").password == "x"
    with pytest.raises(ValidationError):
        ResetPasswordResult(password="")
