import pytest

from app.backends.base import BackendError
from app.backends.diagnostics import NULL_RECORDER, DiagnosticsRecorder


def _clock():
    ticks = iter([1.0, 1.2, 5.0, 5.8])  # start/stop pairs, seconds
    return lambda: next(ticks)


async def test_step_records_name_phase_and_ms_on_success():
    rec = DiagnosticsRecorder(now=_clock())
    async with rec.step("recharge.post", phase="primary"):
        pass
    snap = rec.snapshot()
    assert snap["steps"] == [
        {"name": "recharge.post", "phase": "primary", "http": True,
         "external": False, "ok": True, "ms": 200}
    ]


async def test_step_records_failure_and_reraises():
    rec = DiagnosticsRecorder(now=_clock())
    with pytest.raises(BackendError):
        async with rec.step("recharge.post", phase="primary"):
            raise BackendError("boom")
    step = rec.snapshot()["steps"][0]
    assert step["ok"] is False
    assert step["ms"] == 200


async def test_skip_records_skipped_step():
    rec = DiagnosticsRecorder()
    rec.skip("login.submit", phase="auth")
    step = rec.snapshot()["steps"][0]
    assert step == {"name": "login.submit", "phase": "auth", "http": False,
                    "external": False, "ok": True, "ms": 0, "skipped": True}


async def test_session_event_relogin_is_sticky():
    rec = DiagnosticsRecorder()
    rec.session_event("fresh")
    rec.session_event("relogin")
    rec.session_event("fresh")   # must not downgrade
    assert rec.snapshot()["session_reuse"] == "relogin"


async def test_marks_are_reported():
    rec = DiagnosticsRecorder()
    rec.mark_external_user_id("u:1")
    rec.mark_balance_before(40.0)
    rec.mark_balance_after(90.0)
    snap = rec.snapshot()
    assert snap["external_user_id"] == "u:1"
    assert snap["balance_before"] == 40.0
    assert snap["balance_after"] == 90.0


async def test_null_recorder_is_inert():
    async with NULL_RECORDER.step("x", phase="primary"):
        pass
    NULL_RECORDER.skip("y", phase="auth")
    NULL_RECORDER.session_event("fresh")
    NULL_RECORDER.mark_balance_after(1.0)
    assert NULL_RECORDER.snapshot() == {
        "steps": [], "session_reuse": None,
        "external_user_id": None, "balance_before": None, "balance_after": None,
    }
