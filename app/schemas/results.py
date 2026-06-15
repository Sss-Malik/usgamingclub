# app/schemas/results.py
from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Result(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CreateAccountResult(_Result):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    external_user_id: str | None = None

    @field_validator("external_user_id")
    @classmethod
    def _non_empty_if_present(cls, v: str | None) -> str | None:
        if v is not None and v == "":
            raise ValueError("external_user_id must be non-empty if present")
        return v


class ReadBalanceResult(_Result):
    balance: float = Field(ge=0)


class ResetPasswordResult(_Result):
    password: str = Field(min_length=1)


class RechargeResult(_Result):
    balance: float | None = Field(default=None, ge=0)


class RedeemResult(_Result):
    balance: float | None = Field(default=None, ge=0)


class AgentBalanceResult(_Result):
    agent_balance: float = Field(ge=0)
