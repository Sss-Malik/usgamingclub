import time

import httpx
import pytest
import respx

from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.session import CachedSession, InMemoryCookieSessionStore
from app.backends.diagnostics import DiagnosticsRecorder
from tests.conftest import FakeCaptchaSolver

BASE = "https://os.test"

_LOGIN_PAGE = """
<form><input type="hidden" name="__VIEWSTATE" value="VS_A" />
<input type="hidden" name="__VIEWSTATEGENERATOR" value="CA0B0334" />
<input type="hidden" name="__EVENTVALIDATION" value="EV_A" />
<img src="Tools/VerifyImagePage.aspx?1" /></form>
"""


def _mock_login_chain(cookie_value: str = "COOKIE_NEW"):
    """Stand up the GET-default + GET-captcha + POST-default chain returning success."""
    respx.get(f"{BASE}/default.aspx").mock(
        return_value=httpx.Response(
            200, text=_LOGIN_PAGE,
            headers={"Set-Cookie": f"ASP.NET_SessionId={cookie_value}; path=/"},
        )
    )
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?1").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8FAKE")
    )
    respx.post(f"{BASE}/default.aspx").mock(
        return_value=httpx.Response(301, text="", headers={"Location": "Cashier.aspx"})
    )


def _client(http, store=None, captcha=None, diagnostics=None) -> AspnetCashierClient:
    return AspnetCashierClient(
        base_url=BASE, username="u", password="p",
        http_client=http,
        session_store=store or InMemoryCookieSessionStore(),
        captcha_solver=captcha or FakeCaptchaSolver(),
        game_id=42,
        session_ttl_seconds=1800,
        lock_ttl_seconds=20,
        lock_acquire_timeout_seconds=5.0,
        captcha_login_max_attempts=3,
        driver_prefix="orionstars",
        diagnostics=diagnostics,
    )


@respx.mock
async def test_get_or_login_returns_cached_cookie_when_fresh():
    store = InMemoryCookieSessionStore()
    await store.set(
        42, CachedSession(cookie="CACHED", expires_at=int(time.time()) + 3600),
        ttl_seconds=3600,
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        cookie = await c.get_or_login()
    assert cookie == "CACHED"
    # No HTTP calls were made
    assert len(respx.calls) == 0


@respx.mock
async def test_get_or_login_performs_login_on_cache_miss():
    _mock_login_chain("NEW_COOKIE")
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http)
        cookie = await c.get_or_login()
    assert cookie == "NEW_COOKIE"


@respx.mock
async def test_get_or_login_treats_expired_cache_as_miss():
    store = InMemoryCookieSessionStore()
    await store.set(
        42, CachedSession(cookie="EXPIRED", expires_at=int(time.time()) - 60),
        ttl_seconds=60,
    )
    _mock_login_chain("REFRESHED")
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        cookie = await c.get_or_login()
    assert cookie == "REFRESHED"


# Need TransientBackendError for the session-death test
from app.backends.base import TransientBackendError  # noqa: E402 - import grouped with the new tests

# --- request() ---

_NRE_500 = """
<html><body>Server Error in '/' Application.
<br>System.NullReferenceException: Object reference not set...</body></html>
"""


@respx.mock
async def test_request_attaches_session_cookie_and_accept_language():
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="USE_ME", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    route = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text="0.00@0.00|<html/>")
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        body = await c.request_text("POST", "/Module/AccountManager/AccountsList.aspx",
                                    form={"getscoreuserid": "1"})
    assert body.startswith("0.00@0.00|")
    sent = route.calls.last.request
    assert sent.headers.get("accept-language", "").startswith("en")
    cookie_hdr = sent.headers.get("cookie", "")
    assert "ASP.NET_SessionId=USE_ME" in cookie_hdr


@respx.mock
async def test_request_retries_once_after_session_death_500_nre():
    """First call returns the NRE 500 (dead session). Client clears cache, re-logs in, retries."""
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="DEAD", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    _mock_login_chain("REVIVED")
    route = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        side_effect=[
            httpx.Response(500, text=_NRE_500),
            httpx.Response(200, text="9.99@0.00|<html/>"),
        ]
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        body = await c.request_text("POST", "/Module/AccountManager/AccountsList.aspx",
                                    form={"getscoreuserid": "1"})
    assert body.startswith("9.99@0.00|")
    assert route.call_count == 2
    second = route.calls[-1].request
    assert "ASP.NET_SessionId=REVIVED" in second.headers.get("cookie", "")


@respx.mock
async def test_request_does_not_retry_more_than_once_on_repeated_500():
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="DEAD", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    _mock_login_chain("REVIVED")
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(500, text=_NRE_500),
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        with pytest.raises(TransientBackendError):
            await c.request_text("POST", "/Module/AccountManager/AccountsList.aspx",
                                 form={"getscoreuserid": "1"})


# --- op-facing helpers ---

_ACCOUNTS_LIST_HTML = """
<form id="form1">
  <input type="hidden" name="__VIEWSTATE" value="ALV" />
  <input type="hidden" name="__VIEWSTATEGENERATOR" value="CF7AEB79" />
  <div class="nav">Balance:42</div>
  <table>
    <tr><td><a onclick="updateSelect( '21041615,21219386')">Update</a></td></tr>
  </table>
</form>
"""

_DIALOG_HTML = """
<form id="form1">
  <input type="hidden" name="__VIEWSTATE" value="DLG" />
  <input type="hidden" name="__VIEWSTATEGENERATOR" value="DB3B1D51" />
  <input type="hidden" name="__EVENTVALIDATION" value="DLEV" />
</form>
"""


@respx.mock
async def test_search_returns_uid_gid_pairs():
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="C", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_ACCOUNTS_LIST_HTML)
    )
    route = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_ACCOUNTS_LIST_HTML)
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        pairs = await c.search_account("Saud_Doe892")
    assert pairs == [("21041615", "21219386")]
    body = route.calls.last.request.content.decode()
    assert "__EVENTTARGET=ctl16" in body
    assert "txtSearch=Saud_Doe892" in body
    assert "ShowHideAccount=1" in body
    assert "__VIEWSTATE=ALV" in body
    assert "__EVENTVALIDATION" not in body                 # AccountsList: EnableEventValidation=false


@respx.mock
async def test_get_dialog_url_returns_url_and_token():
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="C", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200,
            text="Module/AccountManager/GrantTreasure.aspx?param=TOKENAAAA|<html/>",
        )
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        url, token = await c.get_dialog_url(tourl=0, uid="21041615", gid="21219386")
    assert url == "Module/AccountManager/GrantTreasure.aspx?param=TOKENAAAA"
    assert token == "TOKENAAAA"


@respx.mock
async def test_submit_dialog_get_then_post_uses_scraped_viewstate():
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="C", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    respx.get(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=TOKEN").mock(
        return_value=httpx.Response(200, text=_DIALOG_HTML)
    )
    route = respx.post(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=TOKEN").mock(
        return_value=httpx.Response(
            200,
            text='<script>showAlter("Confirmed successful","Balance:30");</script>',
        )
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        text = await c.submit_dialog(
            dialog_url="Module/AccountManager/GrantTreasure.aspx?param=TOKEN",
            extra_fields={"txtAddGold": "1", "txtReason": ""},
        )
    assert "Confirmed successful" in text
    body = route.calls.last.request.content.decode()
    assert "__EVENTTARGET=Button1" in body
    assert "__VIEWSTATE=DLG" in body
    assert "__EVENTVALIDATION=DLEV" in body
    assert "txtAddGold=1" in body


@respx.mock
async def test_fetch_agent_balance_widget_returns_int_cents():
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="C", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_ACCOUNTS_LIST_HTML)
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        bal = await c.fetch_agent_balance_dollars()
    assert bal == 42


# --- Pandamaster quirk: missing __VIEWSTATEGENERATOR ---

_PANDAMASTER_LIST_HTML = """
<form id="form1">
  <input type="hidden" name="__VIEWSTATE" value="PVS" />
  <input type="hidden" name="__SCROLLPOSITIONX" value="0" />
  <input type="hidden" name="__SCROLLPOSITIONY" value="0" />
  <div class="nav">Balance:42</div>
  <table>
    <tr><td><a onclick="updateSelect( '11,22')">Update</a></td></tr>
  </table>
</form>
"""


@respx.mock
async def test_search_account_omits_viewstategenerator_when_absent():
    """Pandamaster's AccountsList GET has no __VIEWSTATEGENERATOR. The search POST must
    send exactly the hidden fields that were present — i.e. NOT include VSG with empty value."""
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="C", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_PANDAMASTER_LIST_HTML)
    )
    route = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_PANDAMASTER_LIST_HTML)
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        pairs = await c.search_account("Saud_Doe892")
    assert pairs == [("11", "22")]
    body = route.calls.last.request.content.decode()
    assert "__EVENTTARGET=ctl16" in body
    assert "__VIEWSTATE=PVS" in body
    # Critical: VSG must NOT appear in the body when absent from the GET
    assert "__VIEWSTATEGENERATOR" not in body


@respx.mock
async def test_milkyway_read_balance_omits_viewstategenerator_when_absent():
    """Same Pandamaster quirk for the milkyway-style balance read."""
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="C", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    # GET returns the AccountsList page WITHOUT __VIEWSTATEGENERATOR
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_PANDAMASTER_LIST_HTML)
    )
    # Search POST returns a milkyway-style results row with Balance column
    search_result_html = """
    <table>
      <tr>
        <td><a onclick="updateSelect( '11,22')">U</a></td>
        <td>22</td>
        <td>Saud_Doe892</td>
        <td>Saud</td>
        <td>7.50</td>
        <td>2026-05-30</td>
        <td>2026-06-01</td>
        <td>TestPM159</td>
        <td>Active</td>
      </tr>
    </table>
    """
    route = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=search_result_html)
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        credit_str = await c.milkyway_read_balance(query="Saud_Doe892")
    assert credit_str == "7.50"
    body = route.calls.last.request.content.decode()
    assert "__VIEWSTATEGENERATOR" not in body
    assert "txtSearch=Saud_Doe892" in body


@respx.mock
async def test_submit_dialog_omits_viewstategenerator_when_absent():
    """Dialog pages normally render VSG, but verify the conditional skipping works there too."""
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="C", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    # Dialog HTML missing VSG (hypothetical — defensive guard)
    respx.get(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=TOK").mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="VS" />
                    <input type="hidden" name="__EVENTVALIDATION" value="EV" /></form>""",
        )
    )
    route = respx.post(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=TOK").mock(
        return_value=httpx.Response(
            200, text='<script>showAlter("Confirmed successful","Balance:30");</script>',
        )
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        text = await c.submit_dialog(
            dialog_url="Module/AccountManager/GrantTreasure.aspx?param=TOK",
            extra_fields={"txtAddGold": "1"},
        )
    assert "Confirmed successful" in text
    body = route.calls.last.request.content.decode()
    assert "__VIEWSTATEGENERATOR" not in body
    assert "__VIEWSTATE=VS" in body
    assert "__EVENTVALIDATION=EV" in body


@respx.mock
async def test_request_text_4xx_is_transient_not_terminal():
    """4xx responses that weren't recognized as session-death should be retryable.

    Cloudflare blips, momentary rate limits, and brief auth glitches all surface as 4xx
    on these portals; classifying them as terminal would burn the op. The design doc's
    session-death table pins 'Other 4xx' as transient.
    """
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="C", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(429, text="Too Many Requests"),
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        with pytest.raises(TransientBackendError, match="http_429"):
            await c.request_text("POST", "/Module/AccountManager/AccountsList.aspx",
                                 form={"x": "1"})


# --- diagnostics: session events ---

@respx.mock
async def test_get_or_login_cache_hit_emits_session_hit():
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="CACHED", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, diagnostics=rec)
        cookie = await c.get_or_login()
    assert cookie == "CACHED"
    assert rec.snapshot()["session_reuse"] == "hit"


@respx.mock
async def test_get_or_login_cache_miss_emits_session_fresh():
    _mock_login_chain("NEW_COOKIE")
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        cookie = await _client(http, diagnostics=rec).get_or_login()
    assert cookie == "NEW_COOKIE"
    assert rec.snapshot()["session_reuse"] == "fresh"


@respx.mock
async def test_request_dead_session_retry_records_recovery_step_and_relogin_event():
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="DEAD", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    _mock_login_chain("REVIVED")
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        side_effect=[
            httpx.Response(500, text=_NRE_500),
            httpx.Response(200, text="9.99@0.00|<html/>"),
        ]
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, diagnostics=rec)
        body = await c.request_text(
            "POST", "/Module/AccountManager/AccountsList.aspx",
            form={"getscoreuserid": "1"}, step="balance.getscore_post", phase="primary",
        )
    assert body.startswith("9.99@0.00|")
    snap = rec.snapshot()
    assert snap["session_reuse"] == "relogin"
    names = [s["name"] for s in snap["steps"]]
    assert "recovery" in names
    assert "balance.getscore_post" in names


# --- diagnostics: the six op-facing step names + login sub-steps, forced login ---

@respx.mock
async def test_forced_login_records_login_substeps_and_all_six_op_steps():
    """No pre-seeded session -> get_or_login must perform a fresh captcha login.
    Then exercise every op-facing helper once so all six pinned step names + the
    four login sub-steps show up in a single snapshot."""
    _mock_login_chain("FORCED")
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_ACCOUNTS_LIST_HTML)
    )
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        side_effect=[
            httpx.Response(200, text=_ACCOUNTS_LIST_HTML),      # search_account POST
            httpx.Response(
                200, text="Module/AccountManager/GrantTreasure.aspx?param=TOK|<html/>",
            ),                                                   # get_dialog_url (tourl) POST
            httpx.Response(200, text="9.99@0.00|<html/>"),       # post_getscoreuserid POST
        ]
    )
    respx.get(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=TOK").mock(
        return_value=httpx.Response(200, text=_DIALOG_HTML)
    )
    respx.post(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=TOK").mock(
        return_value=httpx.Response(
            200, text='<script>showAlter("Confirmed successful","Balance:30");</script>',
        )
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, diagnostics=rec)   # no session seeded -> forces login
        pairs = await c.search_account("Saud_Doe892")
        dialog_url, _token = await c.get_dialog_url(tourl=0, uid="21041615", gid="21219386")
        await c.submit_dialog(dialog_url=dialog_url, extra_fields={"txtAddGold": "1", "txtReason": ""})
        await c.post_getscoreuserid("21041615")
    assert pairs == [("21041615", "21219386")]
    snap = rec.snapshot()
    names = [s["name"] for s in snap["steps"]]
    for expected in (
        "resolve.accounts_list_get", "resolve.search_post",
        "dialog.tourl_post", "dialog.get", "dialog.post",
        "balance.getscore_post",
    ):
        assert expected in names, f"{expected!r} missing from recorded steps {names}"
    for expected in ("login.page", "login.captcha_img", "login.captcha_solve", "login.submit"):
        assert expected in names, f"{expected!r} missing from recorded steps {names}"
    captcha_step = next(s for s in snap["steps"] if s["name"] == "login.captcha_solve")
    assert captcha_step["external"] is True
    assert captcha_step["http"] is False
    assert snap["session_reuse"] == "fresh"


@respx.mock
async def test_create_account_helper_records_create_get_and_post_steps():
    """create.get/create.post are named by the caller (the backend), not the client —
    exercise request_text directly the way create_account does, to lock the contract."""
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="C", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    respx.get(__import__("re").compile(r".*CreateAccount\.aspx.*")).mock(
        return_value=httpx.Response(200, text=_DIALOG_HTML)
    )
    respx.post(__import__("re").compile(r".*CreateAccount\.aspx.*")).mock(
        return_value=httpx.Response(200, text='<script>testAlter("Added successfully");</script>')
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, diagnostics=rec)
        await c.request_text("GET", "/Module/AccountManager/CreateAccount.aspx",
                             params={"time": "x"}, step="create.get", phase="primary")
        await c.request_text("POST", "/Module/AccountManager/CreateAccount.aspx",
                             params={"time": "x"}, form={}, step="create.post", phase="primary")
    names = [s["name"] for s in rec.snapshot()["steps"]]
    assert "create.get" in names
    assert "create.post" in names
