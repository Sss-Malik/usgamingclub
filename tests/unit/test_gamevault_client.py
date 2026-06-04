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
        with pytest.raises(TransientBackendError):
            await _client(http).call("/api/external/agentBalance", {})

    respx.post(f"{BASE}/api/external/agentBalance").mock(side_effect=httpx.ConnectTimeout("boom"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _client(http).call("/api/external/agentBalance", {})
