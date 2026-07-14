import time

import httpx
import respx

from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.session import CachedSession, InMemoryCookieSessionStore
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.diagnostics import DiagnosticsRecorder
from app.backends.milkyway.backend import MilkyWayBackend
from tests.conftest import FakeCaptchaSolver

BASE = "https://mw.test"


def _credentials() -> GameCredentials:
    return GameCredentials(
        game_id=43, name="MW Test",
        backend_url=BASE, login_page_url=None,
        backend_username="TestMW159", backend_password="Test@159872!!",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="milkyway",
    )


def _account(external: str | None = None) -> AccountIdentity:
    return AccountIdentity(
        game_account_id=1, user_id=1, game_id=43,
        username="Saud_Doe892", external_user_id=external,
    )


def _ctx(account=None, diagnostics: DiagnosticsRecorder | None = None) -> BackendContext:
    return BackendContext(
        credentials=_credentials(), user_id=1, account=account,
        idempotency_key="idem", account_username="Saud_Doe892",
        diagnostics=diagnostics,
    )


def _make_backend(http, diagnostics: DiagnosticsRecorder | None = None):
    store = InMemoryCookieSessionStore()
    client = AspnetCashierClient(
        base_url=BASE, username="u", password="p",
        http_client=http, session_store=store,
        captcha_solver=FakeCaptchaSolver(),
        game_id=43, session_ttl_seconds=1800,
        lock_ttl_seconds=20, lock_acquire_timeout_seconds=5.0,
        captcha_login_max_attempts=3, driver_prefix="milkyway",
        diagnostics=diagnostics,
    )
    return MilkyWayBackend(client), store


_MW_LIST_HTML = """
<form id="form1">
  <input type="hidden" name="__VIEWSTATE" value="V" />
  <input type="hidden" name="__VIEWSTATEGENERATOR" value="G" />
</form>
"""

_MW_SEARCH_RESULT = """
<table>
  <tr>
    <td><a onclick="updateSelect( '21041615,21219386')">Update</a></td>
    <td>21219386</td>
    <td>Saud_Doe892</td>
    <td>Saud</td>
    <td>456.78</td>
    <td>2026-05-30</td>
    <td>2026-06-01</td>
    <td>TestMW159</td>
    <td>Active</td>
  </tr>
</table>
"""


@respx.mock
async def test_milkyway_read_balance_parses_row_no_getscoreuserid_call():
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_MW_LIST_HTML)
    )
    posts = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_MW_SEARCH_RESULT)
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await store.set(43, CachedSession(cookie="S", expires_at=int(time.time()) + 3600),
                        ttl_seconds=3600)
        result = await backend.read_balance(_ctx(account=_account(external="21041615:21219386")))
    assert result.balance == 456.78
    # Verify the POST body is the ctl16 search (NOT getscoreuserid).
    sent = posts.calls.last.request.content.decode()
    assert "__EVENTTARGET=ctl16" in sent
    assert "getscoreuserid" not in sent
    # Cached external -> GameID portion (more selective) used as txtSearch.
    assert "txtSearch=21219386" in sent


@respx.mock
async def test_milkyway_read_balance_uses_username_when_external_missing():
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_MW_LIST_HTML)
    )
    posts = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_MW_SEARCH_RESULT)
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await store.set(43, CachedSession(cookie="S", expires_at=int(time.time()) + 3600),
                        ttl_seconds=3600)
        result = await backend.read_balance(_ctx(account=_account(external=None)))
    assert result.balance == 456.78
    sent = posts.calls.last.request.content.decode()
    assert "txtSearch=Saud_Doe892" in sent


# --- diagnostics: MilkyWay inherits OrionStars' _player_ids marking on ops it doesn't override ---

@respx.mock
async def test_redeem_marks_external_user_id_from_cached_split():
    posts = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200, text="Module/AccountManager/ChangeTreasure.aspx?param=T|<html/>",
        )
    )
    respx.get(f"{BASE}/Module/AccountManager/ChangeTreasure.aspx?param=T").mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="V" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="19F86183" />
                    <input type="hidden" name="__EVENTVALIDATION" value="EV" /></form>""",
        )
    )
    respx.post(f"{BASE}/Module/AccountManager/ChangeTreasure.aspx?param=T").mock(
        return_value=httpx.Response(
            200, text='<script>showAlter("Confirmed successful","Balance:31");</script>',
        )
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, diagnostics=rec)
        await store.set(43, CachedSession(cookie="S", expires_at=int(time.time()) + 3600),
                        ttl_seconds=3600)
        result = await backend.redeem(_ctx(account=_account(external="111:222"), diagnostics=rec), amount=1)
    assert result.balance is None
    snap = rec.snapshot()
    assert snap["external_user_id"] == "111:222"
    names = [s["name"] for s in snap["steps"]]
    assert "dialog.tourl_post" in names
    assert "dialog.get" in names
    assert "dialog.post" in names
    assert snap["balance_after"] is None                     # no balance marks for aspnet
    tourl_body = posts.calls[0].request.content.decode()
    assert "tourl=1" in tourl_body
