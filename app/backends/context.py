# app/backends/context.py
from dataclasses import dataclass


@dataclass(frozen=True)
class GameCredentials:
    game_id: int
    name: str
    backend_url: str | None
    login_page_url: str | None
    backend_username: str | None
    backend_password: str | None
    api_base_url: str | None
    api_agent_id: str | None
    api_secret_key: str | None
    binding_key: str | None


@dataclass(frozen=True)
class AccountIdentity:
    game_account_id: int
    user_id: int
    game_id: int
    username: str
    external_user_id: str | None


@dataclass(frozen=True)
class BackendContext:
    credentials: GameCredentials
    user_id: int | None
    account: AccountIdentity | None
