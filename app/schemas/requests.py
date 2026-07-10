# app/schemas/requests.py
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: int
    backend_name: str = Field(min_length=1)


class CreateRequest(_In):
    # `username` is the player-chosen base (Arcadia validates it); `full_name` is the legacy seed.
    # Both optional so either side can deploy first — at least one must be present. The endpoint
    # re-sanitizes whichever it uses (generate_username), so neither is trusted verbatim.
    full_name: str | None = None
    username: str | None = None

    @model_validator(mode="after")
    def _need_a_seed(self) -> "CreateRequest":
        if not (self.full_name or self.username):
            raise ValueError("full_name or username is required")
        return self


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
    # Unique per freeplay attempt (the game_recharge row id). Used as the correlation/idempotency
    # key so retries of the same freeplay across different games are distinct ops, and so the
    # webhook can resolve the exact game_recharge row. Optional for backward compatibility.
    freeplay_recharge_id: int | None = None


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
