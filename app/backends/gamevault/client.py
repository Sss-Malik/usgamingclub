# app/backends/gamevault/client.py
import hashlib
import time

import httpx

from app.backends.base import BackendError, TransientBackendError
from app.backends.gamevault.errors import TRANSIENT_CODES, map_code


class GameVaultClient:
    """Transport for the GameVault HTTP API: MD5 auth, multipart POST, envelope parsing."""

    def __init__(self, *, base_url: str, agent_id: str, secret_key: str, http_client: httpx.AsyncClient) -> None:
        self._base_url = base_url.rstrip("/")
        self._agent_id = str(agent_id)
        self._secret_key = secret_key
        self._http = http_client

    def _auth_fields(self) -> dict[str, str]:
        ts = str(int(time.time()))
        token = hashlib.md5(  # noqa: S324 - MD5 is mandated by the GameVault auth scheme, not security
            f"{self._agent_id}:{ts}:{self._secret_key}".encode()
        ).hexdigest()
        return {"agent_id": self._agent_id, "timestamp": ts, "token": token}

    async def call(self, path: str, fields: dict[str, str]) -> dict:
        form = {**self._auth_fields(), **{k: str(v) for k, v in fields.items()}}
        # Force multipart/form-data with plain form fields (filename=None).
        multipart = {k: (None, v) for k, v in form.items()}
        url = f"{self._base_url}{path}"
        try:
            resp = await self._http.post(url, files=multipart)
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"gamevault_transport:{type(exc).__name__}") from exc

        if resp.status_code in (408, 429) or resp.status_code >= 500:
            raise TransientBackendError(f"gamevault_http_{resp.status_code}")
        if resp.status_code >= 300:
            raise BackendError(f"gamevault_http_{resp.status_code}")

        try:
            body = resp.json()
        except ValueError as exc:
            raise TransientBackendError("gamevault_bad_response") from exc

        code = body.get("code")
        if code == 0:
            data = body.get("data")
            return data if isinstance(data, dict) else {}
        reason = map_code(code, body.get("msg", ""))
        if code in TRANSIENT_CODES:
            raise TransientBackendError(reason)
        raise BackendError(reason)
