import time

import httpx
import pytest
import respx

from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.session import CachedSession, InMemoryCookieSessionStore
from app.backends.base import BackendError
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
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


def _ctx(*, account: AccountIdentity | None = None, username: str | None = None) -> BackendContext:
    return BackendContext(
        credentials=_credentials(), user_id=1, account=account,
        idempotency_key="idem-xyz", account_username=username,
    )


def _account(*, external: str | None = None, username: str = "Saud_Doe892") -> AccountIdentity:
    return AccountIdentity(
        game_account_id=1, user_id=1, game_id=42,
        username=username, external_user_id=external,
    )


def _make_backend(http):
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
    )
    return OrionStarsBackend(client), store


async def _seed_session(store):
    await store.set(42, CachedSession(cookie="SESS", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)


# --- read_balance ---

@respx.mock
async def test_read_balance_posts_getscoreuserid_and_returns_cents():
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text="12.34@0.00|<html/>")
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        result = await backend.read_balance(_ctx(account=_account(external="21041615:21219386")))
    assert result.balance_cents == 1234


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
    assert result.balance_cents == 750
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
    assert result.agent_balance_cents == 3100


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
            amount_cents=1200, bonus_cents=1200, total_credit_cents=2400,
        )
    # Spec: omit balance_cents (player balance isn't in this response)
    assert result.balance_cents is None
    sent = submit.calls.last.request.content.decode()
    # The form has one amount field; we must send `total_credit_cents` (principal + bonus),
    # not `amount_cents` — otherwise the bonus is silently dropped on the portal side.
    assert "txtAddGold=24" in sent             # ceil(2400/100) = 24
    assert "txtAddGold=12" not in sent         # regression guard: don't send only the principal
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
                amount_cents=10_000_000, bonus_cents=0, total_credit_cents=10_000_000,
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
        result = await backend.redeem(_ctx(account=_account(external="111:222")), amount_cents=100)
    assert result.balance_cents is None
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
