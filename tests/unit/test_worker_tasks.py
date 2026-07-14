# tests/unit/test_worker_tasks.py
import app.worker.tasks as tasks


async def test_task_delegates_to_executor_with_all_resources(monkeypatch, seeded):
    captured = {}

    async def fake_execute(payload, **kwargs):
        captured["payload"] = payload
        captured["kwargs"] = kwargs

    monkeypatch.setattr(tasks, "execute_operation", fake_execute)

    class FakeClient: ...
    class FakeCache: ...
    class FakeSessionStore: ...
    class FakeRedis: ...

    ctx = {
        "http_client": FakeClient(),
        "session_factory": seeded,
        "result_cache": FakeCache(),
        "session_store": FakeSessionStore(),
        "redis_cache": FakeRedis(),
        "job_try": 1,
    }
    payload = {"action": "read", "type": "READ_BALANCE", "idempotency_key": "read:k",
               "user_id": 42, "backend_name": "milkyway", "username": "player_one"}
    await tasks.execute_operation_task(ctx, payload)

    assert captured["payload"] == payload
    assert captured["kwargs"]["http_client"] is ctx["http_client"]
    assert captured["kwargs"]["session_factory"] is seeded
    assert captured["kwargs"]["result_cache"] is ctx["result_cache"]
    assert captured["kwargs"]["session_store"] is ctx["session_store"]
    assert captured["kwargs"]["redis"] is ctx["redis_cache"]
    assert captured["kwargs"]["retry_blocked"] is False         # first attempt -> not blocked


async def test_task_blocks_retry_when_job_try_exceeds_payload_max_tries(monkeypatch, seeded):
    captured = {}

    async def fake_execute(payload, **kwargs):
        captured["kwargs"] = kwargs

    monkeypatch.setattr(tasks, "execute_operation", fake_execute)
    ctx = {
        "http_client": object(), "session_factory": seeded, "result_cache": object(),
        "session_store": object(), "redis_cache": object(),
        "job_try": 2,                                            # arq is retrying
    }
    # Non-idempotent driver — endpoint embedded _max_tries=1 in the payload.
    payload = {"action": "recharge", "type": "RECHARGE", "idempotency_key": "recharge:t",
               "user_id": 42, "backend_name": "milkyway", "username": "apifull9983654",
               "amount": 100, "_max_tries": 1}
    await tasks.execute_operation_task(ctx, payload)
    assert captured["kwargs"]["retry_blocked"] is True


async def test_task_does_not_block_when_max_tries_absent(monkeypatch, seeded):
    captured = {}

    async def fake_execute(payload, **kwargs):
        captured["kwargs"] = kwargs

    monkeypatch.setattr(tasks, "execute_operation", fake_execute)
    ctx = {
        "http_client": object(), "session_factory": seeded, "result_cache": object(),
        "session_store": object(), "redis_cache": object(),
        "job_try": 5,                                            # even on the 5th retry
    }
    # Idempotent driver — endpoint did NOT embed _max_tries. Worker default behavior applies.
    payload = {"action": "read", "type": "READ_BALANCE", "idempotency_key": "read:k",
               "user_id": 42, "backend_name": "GameVault Demo", "username": "user020301"}
    await tasks.execute_operation_task(ctx, payload)
    assert captured["kwargs"]["retry_blocked"] is False


async def test_worker_passes_job_try_as_attempt(monkeypatch, seeded):
    captured = {}

    async def fake_execute(payload, **kwargs):
        captured["kwargs"] = kwargs

    monkeypatch.setattr(tasks, "execute_operation", fake_execute)
    ctx = {
        "http_client": object(), "session_factory": seeded, "result_cache": object(),
        "session_store": object(), "redis_cache": object(),
        "job_try": 3,
    }
    payload = {"action": "read", "type": "READ_BALANCE", "idempotency_key": "read:k",
               "user_id": 42, "backend_name": "milkyway", "username": "player_one"}
    await tasks.execute_operation_task(ctx, payload)
    assert captured["kwargs"]["attempt"] == 3
