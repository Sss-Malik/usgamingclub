# app/schemas/operations.py
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")
    idempotency_key: str = Field(min_length=1)


class CreateAccountOp(_Base):
    type: Literal["CREATE_ACCOUNT"]
    user_id: int
    game_id: int
    game_account_id: None = None
    account_username: str = Field(min_length=1)


class ReadBalanceOp(_Base):
    type: Literal["READ_BALANCE"]
    user_id: int
    game_id: int
    game_account_id: int


class ResetPasswordOp(_Base):
    type: Literal["RESET_PASSWORD"]
    user_id: int
    game_id: int
    game_account_id: int


class RechargeOp(_Base):
    type: Literal["RECHARGE"]
    user_id: int
    game_id: int
    game_account_id: int
    amount_cents: int = Field(ge=0)
    bonus_cents: int = Field(ge=0)
    total_credit_cents: int = Field(ge=0)


class RedeemOp(_Base):
    type: Literal["REDEEM"]
    user_id: int
    game_id: int
    game_account_id: int
    amount_cents: int = Field(ge=0)


class AgentBalanceOp(_Base):
    type: Literal["AGENT_BALANCE"]
    game_id: int


OperationRequest = Annotated[
    Union[
        CreateAccountOp,
        ReadBalanceOp,
        ResetPasswordOp,
        RechargeOp,
        RedeemOp,
        AgentBalanceOp,
    ],
    Field(discriminator="type"),
]

operation_adapter: TypeAdapter[OperationRequest] = TypeAdapter(OperationRequest)
