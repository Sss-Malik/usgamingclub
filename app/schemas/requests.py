# app/schemas/requests.py
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: int
    backend_name: str = Field(min_length=1)


class CreateRequest(_In):
    full_name: str = Field(min_length=1)


class RechargeRequest(_In):
    username: str = Field(min_length=1)
    amount: int = Field(ge=0)
    transaction_id: str = Field(min_length=1)


class WithdrawRequest(_In):
    username: str = Field(min_length=1)
    amount: int = Field(ge=0)
    redeem_id: int


class ResetPasswordRequest(_In):
    username: str = Field(min_length=1)
    reset_password_id: int


class FreeplayRequest(_In):
    username: str = Field(min_length=1)
    amount: int = Field(ge=0)
    freeplay_id: int


class ReadRequest(_In):
    username: str = Field(min_length=1)
    read_id: int


class Operation(BaseModel):
    """Normalized internal op carried through arq → executor → webhook builder."""

    model_config = ConfigDict(extra="ignore")
    action: Literal["create", "recharge", "redeem", "reset_password", "freeplay", "read"]
    type: Literal[
        "CREATE_ACCOUNT", "RECHARGE", "REDEEM", "RESET_PASSWORD", "FREEPLAY", "READ_BALANCE"
    ]
    idempotency_key: str = Field(min_length=1)
    user_id: int
    backend_name: str = Field(min_length=1)
    username: str | None = None
    account_username: str | None = None
    amount: int | None = Field(default=None, ge=0)
    correlation: dict[str, str | int] = Field(default_factory=dict)
