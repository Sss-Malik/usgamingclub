# tests/unit/test_gamevault_client.py
import hashlib

import httpx
import pytest
import respx

from app.backends.base import BackendError, TransientBackendError
from app.backends.gamevault.client import GameVaultClient

BASE = "https://gv.test"


def _client(http):
    return GameVaultClient(base_url=BASE, agent_id="11", secret_key="gvsecret", http_client=http)


@respx.mock
async def test_call_signs_and_returns_data_on_code_0(monkeypatch):
    monkeypatch.setattr("app.backends.gamevault.client.time.time", lambda: 1709867667.0)
    route = respx.post(f"{BASE}/api/external/userBalance").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "Success", "data": {"user_balance": "60"}, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        data = await _client(http).call("/api/external/userBalance", {"user_id": "88880212"})
    assert data == {"user_balance": "60"}
    sent = route.calls.last.request
    body = sent.content.decode()
    expected_token = hashlib.md5(b"11:1709867667:gvsecret").hexdigest()  # noqa: S324 - GameVault protocol
    assert "multipart/form-data" in sent.headers["content-type"]
    assert 'name="agent_id"' in body and "11" in body
    assert 'name="timestamp"' in body and "1709867667" in body
    assert expected_token in body
    assert 'name="user_id"' in body and "88880212" in body


@respx.mock
async def test_business_code_raises_backend_error():
    respx.post(f"{BASE}/api/external/withdraw").mock(
        return_value=httpx.Response(200, json={"code": 10, "msg": "User is in game", "data": None, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _client(http).call("/api/external/withdraw", {"user_id": "1"})
    assert ei.value.reason == "gamevault:10:user_in_game"
    assert not isinstance(ei.value, TransientBackendError)
    assert ei.value.provider_http_status == 200
    assert ei.value.provider_code == 10
    assert ei.value.provider_message == "User is in game"


@respx.mock
async def test_business_error_carries_provider_fields():
    respx.post(f"{BASE}/api/external/recharge").mock(
        return_value=httpx.Response(200, json={"code": 7, "msg": "user balance not enough", "data": None, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _client(http).call("/api/external/recharge", {"user_id": "1"},
                                      step="recharge.post", phase="primary")
    err = ei.value
    assert err.provider_code == 7
    assert err.provider_message == "user balance not enough"
    assert err.provider_http_status == 200


@respx.mock
async def test_transient_business_code_raises_transient():
    respx.post(f"{BASE}/api/external/recharge").mock(
        return_value=httpx.Response(200, json={"code": 21, "msg": "System failed", "data": None, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _client(http).call("/api/external/recharge", {"user_id": "1"})


@respx.mock
async def test_http_5xx_and_timeout_are_transient():
    respx.post(f"{BASE}/api/external/agentBalance").mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError) as ei:
            await _client(http).call("/api/external/agentBalance", {})
    assert ei.value.provider_http_status == 503

    respx.post(f"{BASE}/api/external/agentBalance").mock(side_effect=httpx.ConnectTimeout("boom"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError) as ei:
            await _client(http).call("/api/external/agentBalance", {})
    assert ei.value.provider_http_status is None  # no response object on a transport-level error


@respx.mock
async def test_call_records_named_step_on_diag_recorder():
    from app.backends.diagnostics import DiagnosticsRecorder

    respx.post(f"{BASE}/api/external/userBalance").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"user_balance": "1"}, "count": 0})
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient() as http:
        client = GameVaultClient(base_url=BASE, agent_id="11", secret_key="gvsecret",
                                  http_client=http, diagnostics=rec)
        await client.call("/api/external/userBalance", {"user_id": "1"},
                           step="balance.read", phase="primary")
    names = [s["name"] for s in rec.snapshot()["steps"]]
    assert names == ["balance.read"]


@respx.mock
async def test_http_429_and_408_are_transient():
    for status in (408, 429):
        respx.post(f"{BASE}/api/external/agentBalance").mock(return_value=httpx.Response(status))
        async with httpx.AsyncClient() as http:
            with pytest.raises(TransientBackendError):
                await _client(http).call("/api/external/agentBalance", {})
