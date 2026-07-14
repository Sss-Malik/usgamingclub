import time
from contextlib import asynccontextmanager

_SESSION_RANK = {None: 0, "hit": 1, "fresh": 2, "relogin": 3}


class DiagnosticsRecorder:
    """Mutable per-operation recorder for step timing, session reuse, and success marks.

    Lives on the executor stack and is threaded through BackendContext + clients. Survives
    exception unwinding, so a failing op still yields the steps that ran before the raise.
    """

    def __init__(self, *, now=time.monotonic) -> None:
        self._now = now
        self._steps: list[dict] = []
        self._session_reuse: str | None = None
        self._external_user_id = None
        self._balance_before = None
        self._balance_after = None

    @asynccontextmanager
    async def step(self, name: str, *, phase: str, http: bool = True, external: bool = False):
        start = self._now()
        ok = True
        try:
            yield self
        except BaseException:
            ok = False
            raise
        finally:
            ms = round((self._now() - start) * 1000)
            self._steps.append({
                "name": name, "phase": phase, "http": http,
                "external": external, "ok": ok, "ms": ms,
            })

    def skip(self, name: str, *, phase: str) -> None:
        self._steps.append({
            "name": name, "phase": phase, "http": False,
            "external": False, "ok": True, "ms": 0, "skipped": True,
        })

    def session_event(self, kind: str) -> None:
        if _SESSION_RANK.get(kind, 0) >= _SESSION_RANK.get(self._session_reuse, 0):
            self._session_reuse = kind

    def mark_external_user_id(self, value) -> None:
        self._external_user_id = value

    def mark_balance_before(self, value) -> None:
        self._balance_before = value

    def mark_balance_after(self, value) -> None:
        self._balance_after = value

    def snapshot(self) -> dict:
        return {
            "steps": list(self._steps),
            "session_reuse": self._session_reuse,
            "external_user_id": self._external_user_id,
            "balance_before": self._balance_before,
            "balance_after": self._balance_after,
        }


class _NullRecorder(DiagnosticsRecorder):
    """No-op recorder used when diagnostics is not wired (direct client construction, tests)."""

    @asynccontextmanager
    async def step(self, name: str, *, phase: str, http: bool = True, external: bool = False):
        yield self

    def skip(self, name: str, *, phase: str) -> None:
        return None

    def session_event(self, kind: str) -> None:
        return None

    def mark_external_user_id(self, value) -> None:
        return None

    def mark_balance_before(self, value) -> None:
        return None

    def mark_balance_after(self, value) -> None:
        return None


NULL_RECORDER = _NullRecorder()
