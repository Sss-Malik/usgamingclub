# app/backends/registry.py
from app.backends.base import GameBackend
from app.backends.mock.backend import MockBackend
from app.config import get_settings


def get_backend(game_id: int) -> GameBackend:
    """Resolve the backend for a game. Phase 1: every game uses the MockBackend.

    Later phases map specific game_ids to real backend modules here.
    """
    settings = get_settings()
    return MockBackend(fail=settings.mock_force_fail, fail_reason=settings.mock_force_fail_reason)
