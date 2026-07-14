import time

import httpx
import pytest
import respx

from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.session import CachedSession, InMemoryCookieSessionStore
from app.backends.base import BackendError
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.diagnostics import DiagnosticsRecorder
from app.backends.orionstars.backend import OrionStarsBackend
from tests.conftest import FakeCaptchaSolver

BASE = "https://os.test"


def _credentials() -> GameCredentials:
    return GameCredentials(
        game_id=42, name="OS Test",
        backend_url=BASE, login_page_url=None,
        backend_username="TestOS159", backend_password="Test@159872!!",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="orionstars",
    )


def _ctx(
    *, account: AccountIdentity | None = None, username: str | None = None,
    diagnostics: DiagnosticsRecorder | None = None,
) -> BackendContext:
    return BackendContext(
        credentials=_credentials(), user_id=1, account=account,
        idempotency_key="idem-xyz", account_username=username,
        diagnostics=diagnostics,
    )


def _account(*, external: str | None = None, username: str = "Saud_Doe892") -> AccountIdentity:
    return AccountIdentity(
        game_account_id=1, user_id=1, game_id=42,
        username=username, external_user_id=external,
    )


def _make_backend(http, diagnostics: DiagnosticsRecorder | None = None):
    store = InMemoryCookieSessionStore()
    # Pre-seed a session so individual op tests don't have to mock the login chain.
    import asyncio
    asyncio.get_event_loop()  # ensures loop binding for direct .set on the in-memory store
    client = AspnetCashierClient(
        base_url=BASE, username="u", password="p",
        http_client=http, session_store=store,
        captcha_solver=FakeCaptchaSolver(),
        game_id=42, session_ttl_seconds=1800,
        lock_ttl_seconds=20, lock_acquire_timeout_seconds=5.0,
        captcha_login_max_attempts=3, driver_prefix="orionstars",
        diagnostics=diagnostics,
    )
    return OrionStarsBackend(client), store


def _mock_login_chain(cookie_value: str = "COOKIE_NEW"):
    """Stand up the GET-default + GET-captcha + POST-default chain returning success."""
    _LOGIN_PAGE = """
    <form><input type="hidden" name="__VIEWSTATE" value="VS_A" />
    <input type="hidden" name="__VIEWSTATEGENERATOR" value="CA0B0334" />
    <input type="hidden" name="__EVENTVALIDATION" value="EV_A" />
    <img src="Tools/VerifyImagePage.aspx?1" /></form>
    """
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
        return_value=httpx.Response(301, text="", headers={"Location": "Store.aspx"})
    )


async def _seed_session(store):
    await store.set(42, CachedSession(cookie="SESS", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)


# --- read_balance ---

@respx.mock
async def test_read_balance_posts_getscoreuserid_and_returns_dollars():
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text="12.34@0.00|<html/>")
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        result = await backend.read_balance(_ctx(account=_account(external="21041615:21219386")))
    assert result.balance == 12.34


@respx.mock
async def test_read_balance_searches_when_external_user_id_missing():
    """No external_user_id -> search by username first to obtain UID:GID, then getscoreuserid."""
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="V" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="G" /></form>""",
        )
    )
    posts = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        side_effect=[
            # 1: search response with one row
            httpx.Response(
                200,
                text="""<table><tr><td><a onclick="updateSelect( '111,222')">Update</a></td></tr></table>""",
            ),
            # 2: getscoreuserid response
            httpx.Response(200, text="7.50@0.00|<html/>"),
        ]
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        result = await backend.read_balance(_ctx(account=_account(external=None)))
    assert result.balance == 7.5
    # Confirm last POST used the UID returned by the search
    last_body = posts.calls[-1].request.content.decode()
    assert "getscoreuserid=111" in last_body


# --- agent_balance ---

@respx.mock
async def test_agent_balance_scrapes_widget():
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text='<div>Balance:31</div>')
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        result = await backend.agent_balance(_ctx())
    assert result.agent_balance == 31.0


# --- recharge ---

@respx.mock
async def test_recharge_full_flow_success():
    # tourl POST
    posts = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200,
            text="Module/AccountManager/GrantTreasure.aspx?param=TOK|<html/>",
        )
    )
    # GET dialog
    respx.get(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=TOK").mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="GTV" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="DB3B1D51" />
                    <input type="hidden" name="__EVENTVALIDATION" value="GTEV" /></form>""",
        )
    )
    # POST dialog
    submit = respx.post(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=TOK").mock(
        return_value=httpx.Response(
            200, text='<script>showAlter("Confirmed successful","Balance:30");</script>',
        )
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        result = await backend.recharge(
            _ctx(account=_account(external="21041615:21219386")),
            amount=50,
        )
    # Spec: omit balance (player balance isn't in this response)
    assert result.balance is None
    sent = submit.calls.last.request.content.decode()
    # Wire value: txtAddGold=50
    assert "txtAddGold=50" in sent
    assert "__EVENTTARGET=Button1" in sent
    # tourl POST sent the right indices
    tourl_body = posts.calls[0].request.content.decode()
    assert "tourl=0" in tourl_body
    assert "getpassuid=21041615" in tourl_body
    assert "getpassgid=21219386" in tourl_body


@respx.mock
async def test_recharge_insufficient_agent_funds_raises_terminal():
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200, text="Module/AccountManager/GrantTreasure.aspx?param=T|<html/>",
        )
    )
    respx.get(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=T").mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="V" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="G" />
                    <input type="hidden" name="__EVENTVALIDATION" value="EV" /></form>""",
        )
    )
    respx.post(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=T").mock(
        return_value=httpx.Response(
            200, text='<script>showAlter("Sorry, the surplus money is insufficient!");</script>',
        )
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        with pytest.raises(BackendError) as ei:
            await backend.recharge(
                _ctx(account=_account(external="111:222")),
                amount=10_000,
            )
    assert ei.value.reason == "orionstars:insufficient_agent_funds"


# --- redeem ---

@respx.mock
async def test_redeem_success_uses_ChangeTreasure_and_tourl_1():
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
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        result = await backend.redeem(_ctx(account=_account(external="111:222")), amount=1)
    assert result.balance is None
    tourl_body = posts.calls[0].request.content.decode()
    assert "tourl=1" in tourl_body


# --- reset_password ---

@respx.mock
async def test_reset_password_success():
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200, text="Module/AccountManager/ResetPassWord.aspx?param=T|<html/>",
        )
    )
    respx.get(f"{BASE}/Module/AccountManager/ResetPassWord.aspx?param=T").mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="V" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="C02DB422" />
                    <input type="hidden" name="__EVENTVALIDATION" value="EV" /></form>""",
        )
    )
    submit = respx.post(f"{BASE}/Module/AccountManager/ResetPassWord.aspx?param=T").mock(
        return_value=httpx.Response(200, text='<script>showAlter("Modified success!");</script>')
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        result = await backend.reset_password(_ctx(account=_account(external="111:222")))
    assert result.password and len(result.password) >= 5
    sent = submit.calls.last.request.content.decode()
    assert f"txtConfirmPass={result.password}" in sent
    assert f"txtSureConfirmPass={result.password}" in sent


# --- create_account ---

@respx.mock
async def test_create_account_does_followup_search_to_pack_external_user_id():
    respx.get(__import__("re").compile(r".*CreateAccount\.aspx.*")).mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="V" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="0E9FD35B" />
                    <input type="hidden" name="__EVENTVALIDATION" value="EV" /></form>""",
        )
    )
    respx.post(__import__("re").compile(r".*CreateAccount\.aspx.*")).mock(
        return_value=httpx.Response(
            200, text='<script>testAlter("Added successfully");</script>',
        )
    )
    # follow-up search: GET AccountsList -> POST search
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="ALV" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="G" /></form>""",
        )
    )
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200,
            text="""<table><tr><td><a onclick="updateSelect( '99988877,77766655')">U</a></td></tr></table>""",
        )
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        result = await backend.create_account(_ctx(username="ApiTest_0530"))
    assert result.username == "ApiTest_0530"
    assert result.external_user_id == "99988877:77766655"
    assert result.password


@respx.mock
async def test_create_account_existing_account_raises_terminal():
    respx.get(__import__("re").compile(r".*CreateAccount\.aspx.*")).mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="V" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="0E9FD35B" />
                    <input type="hidden" name="__EVENTVALIDATION" value="EV" /></form>""",
        )
    )
    respx.post(__import__("re").compile(r".*CreateAccount\.aspx.*")).mock(
        return_value=httpx.Response(
            200,
            text='<script>testAlter("The account number already exists, please re-enter it!");</script>',
        )
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        with pytest.raises(BackendError) as ei:
            await backend.create_account(_ctx(username="duplicate"))
    assert ei.value.reason == "orionstars:account_exists"


# --- diagnostics: marks + step names ---

@respx.mock
async def test_create_account_marks_external_user_id_and_records_create_steps():
    respx.get(__import__("re").compile(r".*CreateAccount\.aspx.*")).mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="V" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="0E9FD35B" />
                    <input type="hidden" name="__EVENTVALIDATION" value="EV" /></form>""",
        )
    )
    respx.post(__import__("re").compile(r".*CreateAccount\.aspx.*")).mock(
        return_value=httpx.Response(
            200, text='<script>testAlter("Added successfully");</script>',
        )
    )
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="ALV" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="G" /></form>""",
        )
    )
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200,
            text="""<table><tr><td><a onclick="updateSelect( '99988877,77766655')">U</a></td></tr></table>""",
        )
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, diagnostics=rec)
        await _seed_session(store)
        result = await backend.create_account(_ctx(username="ApiTest_0530", diagnostics=rec))
    assert result.external_user_id == "99988877:77766655"
    snap = rec.snapshot()
    assert snap["external_user_id"] == "99988877:77766655"
    names = [s["name"] for s in snap["steps"]]
    assert "create.get" in names
    assert "create.post" in names
    assert snap["balance_after"] is None                     # aspnet never marks balance
    assert snap["balance_before"] is None


@respx.mock
async def test_player_ids_marks_external_user_id_from_cached_split():
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text="12.34@0.00|<html/>")
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, diagnostics=rec)
        await _seed_session(store)
        await backend.read_balance(_ctx(account=_account(external="21041615:21219386"), diagnostics=rec))
    assert rec.snapshot()["external_user_id"] == "21041615:21219386"


@respx.mock
async def test_player_ids_marks_external_user_id_from_search_fallback():
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="V" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="G" /></form>""",
        )
    )
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        side_effect=[
            httpx.Response(
                200,
                text="""<table><tr><td><a onclick="updateSelect( '111,222')">Update</a></td></tr></table>""",
            ),
            httpx.Response(200, text="7.50@0.00|<html/>"),
        ]
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, diagnostics=rec)
        await _seed_session(store)
        await backend.read_balance(_ctx(account=_account(external=None), diagnostics=rec))
    assert rec.snapshot()["external_user_id"] == "111:222"


@respx.mock
async def test_recharge_with_forced_login_records_session_fresh_and_op_steps():
    _mock_login_chain("FORCED")
    posts = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200, text="Module/AccountManager/GrantTreasure.aspx?param=TOK|<html/>",
        )
    )
    respx.get(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=TOK").mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="GTV" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="DB3B1D51" />
                    <input type="hidden" name="__EVENTVALIDATION" value="GTEV" /></form>""",
        )
    )
    respx.post(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=TOK").mock(
        return_value=httpx.Response(
            200, text='<script>showAlter("Confirmed successful","Balance:30");</script>',
        )
    )
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, _store = _make_backend(http, diagnostics=rec)
        # No _seed_session(store) call -> get_or_login must perform a fresh captcha login.
        result = await backend.recharge(
            _ctx(account=_account(external="21041615:21219386"), diagnostics=rec),
            amount=50,
        )
    assert result.balance is None
    _ = posts
    snap = rec.snapshot()
    assert snap["session_reuse"] == "fresh"
    assert snap["external_user_id"] == "21041615:21219386"
    assert snap["balance_after"] is None
    names = [s["name"] for s in snap["steps"]]
    for expected in ("dialog.tourl_post", "dialog.get", "dialog.post",
                     "login.page", "login.captcha_img", "login.captcha_solve", "login.submit"):
        assert expected in names, f"{expected!r} missing from {names}"
