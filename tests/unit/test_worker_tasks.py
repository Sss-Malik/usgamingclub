# tests/unit/test_worker_tasks.py
import app.worker.tasks as tasks


async def test_task_delegates_to_executor(monkeypatch, seeded):
    captured = {}

    async def fake_execute(payload, **kwargs):
        captured["payload"] = payload
        captured["kwargs"] = kwargs

    monkeypatch.setattr(tasks, "execute_operation", fake_execute)

    class FakeClient: ...
    class FakeCache: ...

    ctx = {"http_client": FakeClient(), "session_factory": seeded, "result_cache": FakeCache()}
    payload = {"idempotency_key": "k", "type": "READ_BALANCE", "user_id": 42, "game_id": 7, "game_account_id": 1001}
    await tasks.execute_operation_task(ctx, payload)

    assert captured["payload"] == payload
    assert captured["kwargs"]["http_client"] is ctx["http_client"]
    assert captured["kwargs"]["session_factory"] is seeded
    assert captured["kwargs"]["result_cache"] is ctx["result_cache"]
