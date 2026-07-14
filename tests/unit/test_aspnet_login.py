import httpx
import pytest
import respx

from app.backends.base import BackendError
from app.backends._aspnet_cashier.login import login
from app.backends.diagnostics import DiagnosticsRecorder
from tests.conftest import FakeCaptchaSolver

BASE = "https://os.test"

_LOGIN_PAGE = """
<form id="form1" method="post" action="default.aspx">
  <input type="hidden" name="__VIEWSTATE" value="VS_A" />
  <input type="hidden" name="__VIEWSTATEGENERATOR" value="CA0B0334" />
  <input type="hidden" name="__EVENTVALIDATION" value="EV_A" />
  <img id="img1" src="Tools/VerifyImagePage.aspx?12345" />
</form>
"""


def _login_page_response():
    return httpx.Response(
        200,
        text=_LOGIN_PAGE,
        headers={
            # ASP.NET issues this on the first GET
            "Set-Cookie": "ASP.NET_SessionId=COOKIE_FIRST; path=/; HttpOnly",
        },
    )


def _captcha_image_response():
    return httpx.Response(200, content=b"\xff\xd8FAKE_JPEG", headers={"Content-Type": "image/jpeg"})


def _login_success_response():
    return httpx.Response(
        301, text="", headers={"Location": "Cashier.aspx"}
    )


def _login_success_store_response():
    # OrionStars redirects a successful login to Store.aspx (not Cashier.aspx).
    return httpx.Response(
        301, text="", headers={"Location": "Store.aspx"}
    )


def _login_bad_captcha_response():
    return httpx.Response(
        301, text="", headers={"Location": "default.aspx?errtype=verifycode"}
    )


def _login_bad_creds_response():
    return httpx.Response(
        301, text="", headers={"Location": "default.aspx?errtype=errorNamePassowrd"}
    )


# --- happy path ---

@respx.mock
async def test_login_happy_path_returns_session_cookie():
    respx.get(f"{BASE}/default.aspx").mock(return_value=_login_page_response())
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?12345").mock(return_value=_captcha_image_response())
    post = respx.post(f"{BASE}/default.aspx").mock(return_value=_login_success_response())

    async with httpx.AsyncClient(base_url=BASE) as http:
        cookie = await login(
            http=http, base_url=BASE,
            username="TestOS159", password="Test@159872!!",
            captcha_solver=FakeCaptchaSolver(answers=["34596"]),
            max_attempts=3,
        )

    assert cookie == "COOKIE_FIRST"
    body = post.calls.last.request.content.decode()
    assert "txtLoginName=TestOS159" in body
    assert "txtVerifyCode=34596" in body
    assert "__VIEWSTATE=VS_A" in body
    assert "__EVENTVALIDATION=EV_A" in body
    assert "ddlRole=0" in body


@respx.mock
async def test_login_success_on_store_aspx_landing():
    """OrionStars redirects a successful login to Store.aspx, not Cashier.aspx.

    Regression for the production bug where OrionStars logins were misclassified as
    `login_failed_unmapped_errtype:''` because the success check only accepted Cashier.aspx.
    """
    respx.get(f"{BASE}/default.aspx").mock(return_value=_login_page_response())
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?12345").mock(return_value=_captcha_image_response())
    respx.post(f"{BASE}/default.aspx").mock(return_value=_login_success_store_response())

    async with httpx.AsyncClient(base_url=BASE) as http:
        cookie = await login(
            http=http, base_url=BASE, username="u", password="p",
            captcha_solver=FakeCaptchaSolver(answers=["34596"]), max_attempts=1,
            driver_prefix="orionstars",
        )
    assert cookie == "COOKIE_FIRST"


@respx.mock
async def test_login_includes_accept_language_on_every_request():
    respx.get(f"{BASE}/default.aspx").mock(return_value=_login_page_response())
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?12345").mock(return_value=_captcha_image_response())
    respx.post(f"{BASE}/default.aspx").mock(return_value=_login_success_response())
    async with httpx.AsyncClient(base_url=BASE) as http:
        await login(http=http, base_url=BASE, username="u", password="p",
                    captcha_solver=FakeCaptchaSolver(), max_attempts=1)
    for call in respx.calls:
        assert call.request.headers.get("accept-language", "").startswith("en")


# --- captcha retry ---

@respx.mock
async def test_login_retries_on_verifycode_with_fresh_image_and_viewstate():
    get_default = respx.get(f"{BASE}/default.aspx").mock(return_value=_login_page_response())
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?12345").mock(return_value=_captcha_image_response())
    posts = respx.post(f"{BASE}/default.aspx").mock(
        side_effect=[_login_bad_captcha_response(), _login_success_response()]
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        cookie = await login(
            http=http, base_url=BASE, username="u", password="p",
            captcha_solver=FakeCaptchaSolver(answers=["WRONG", "34596"]),
            max_attempts=3,
        )
    assert cookie == "COOKIE_FIRST"
    # GET /default.aspx happened twice — once per attempt — guaranteeing fresh viewstate.
    assert get_default.call_count == 2
    assert posts.call_count == 2


@respx.mock
async def test_login_raises_after_max_attempts_of_verifycode():
    respx.get(f"{BASE}/default.aspx").mock(return_value=_login_page_response())
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?12345").mock(return_value=_captcha_image_response())
    respx.post(f"{BASE}/default.aspx").mock(return_value=_login_bad_captcha_response())
    async with httpx.AsyncClient(base_url=BASE) as http:
        with pytest.raises(BackendError, match="captcha_failed_max_attempts"):
            await login(
                http=http, base_url=BASE, username="u", password="p",
                captcha_solver=FakeCaptchaSolver(answers=["WRONG"]),
                max_attempts=2,
                driver_prefix="orionstars",
            )


# --- terminal failures ---

@respx.mock
async def test_login_terminal_on_bad_credentials():
    respx.get(f"{BASE}/default.aspx").mock(return_value=_login_page_response())
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?12345").mock(return_value=_captcha_image_response())
    respx.post(f"{BASE}/default.aspx").mock(return_value=_login_bad_creds_response())
    async with httpx.AsyncClient(base_url=BASE) as http:
        with pytest.raises(BackendError) as ei:
            await login(
                http=http, base_url=BASE, username="u", password="p",
                captcha_solver=FakeCaptchaSolver(), max_attempts=3,
                driver_prefix="orionstars",
            )
    assert ei.value.reason == "orionstars:login_failed:bad_credentials"


@respx.mock
async def test_login_terminal_failure_sets_provider_code_to_errtype():
    respx.get(f"{BASE}/default.aspx").mock(return_value=_login_page_response())
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?12345").mock(return_value=_captcha_image_response())
    respx.post(f"{BASE}/default.aspx").mock(return_value=_login_bad_creds_response())
    async with httpx.AsyncClient(base_url=BASE) as http:
        with pytest.raises(BackendError) as ei:
            await login(
                http=http, base_url=BASE, username="u", password="p",
                captcha_solver=FakeCaptchaSolver(), max_attempts=3,
                driver_prefix="orionstars",
            )
    assert ei.value.provider_code == "errorNamePassowrd"


# --- regression: cookie jar must be cleared between retries ---

@respx.mock
async def test_login_retry_does_not_send_stale_session_cookie():
    """Regression guard for Issue 1: each retry attempt must start with empty cookies."""
    # First attempt: server issues COOKIE_FIRST then 301 verifycode
    # Second attempt: server issues COOKIE_SECOND then 301 success
    page_responses = iter([
        httpx.Response(200, text=_LOGIN_PAGE,
                       headers={"Set-Cookie": "ASP.NET_SessionId=COOKIE_FIRST; path=/"}),
        httpx.Response(200, text=_LOGIN_PAGE,
                       headers={"Set-Cookie": "ASP.NET_SessionId=COOKIE_SECOND; path=/"}),
    ])
    get_default = respx.get(f"{BASE}/default.aspx").mock(side_effect=lambda req: next(page_responses))
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?12345").mock(return_value=_captcha_image_response())
    respx.post(f"{BASE}/default.aspx").mock(
        side_effect=[_login_bad_captcha_response(), _login_success_response()]
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        cookie = await login(
            http=http, base_url=BASE, username="u", password="p",
            captcha_solver=FakeCaptchaSolver(answers=["WRONG", "OK"]),
            max_attempts=3,
        )
    assert cookie == "COOKIE_SECOND"
    # Second GET /default.aspx must NOT carry COOKIE_FIRST in its Cookie header.
    second_get = [c for c in respx.calls if c.request.method == "GET"
                  and c.request.url.path == "/default.aspx"][1]
    cookie_hdr = second_get.request.headers.get("cookie", "")
    assert "COOKIE_FIRST" not in cookie_hdr, (
        f"Stale session cookie bled into the second login attempt: {cookie_hdr!r}"
    )
    assert get_default.call_count == 2


@respx.mock
async def test_login_empty_errtype_raises_transient_not_terminal():
    respx.get(f"{BASE}/default.aspx").mock(return_value=_login_page_response())
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?12345").mock(return_value=_captcha_image_response())
    # Redirect with no errtype query at all — server hiccup
    respx.post(f"{BASE}/default.aspx").mock(
        return_value=httpx.Response(301, text="", headers={"Location": "default.aspx"})
    )
    from app.backends.base import TransientBackendError
    async with httpx.AsyncClient(base_url=BASE) as http:
        with pytest.raises(TransientBackendError, match="login_failed_unmapped_errtype"):
            await login(
                http=http, base_url=BASE, username="u", password="p",
                captcha_solver=FakeCaptchaSolver(), max_attempts=1,
                driver_prefix="orionstars",
            )


@respx.mock
async def test_login_handles_absolute_path_captcha_src():
    page = _LOGIN_PAGE.replace(
        'src="Tools/VerifyImagePage.aspx?12345"',
        'src="/Tools/VerifyImagePage.aspx?12345"',
    )
    respx.get(f"{BASE}/default.aspx").mock(
        return_value=httpx.Response(200, text=page,
                                    headers={"Set-Cookie": "ASP.NET_SessionId=ABS; path=/"})
    )
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?12345").mock(return_value=_captcha_image_response())
    respx.post(f"{BASE}/default.aspx").mock(return_value=_login_success_response())
    async with httpx.AsyncClient(base_url=BASE) as http:
        cookie = await login(
            http=http, base_url=BASE, username="u", password="p",
            captcha_solver=FakeCaptchaSolver(answers=["34596"]), max_attempts=1,
        )
    assert cookie == "ABS"


# --- diagnostics: login sub-steps ---

@respx.mock
async def test_login_records_four_diagnostics_substeps():
    respx.get(f"{BASE}/default.aspx").mock(return_value=_login_page_response())
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?12345").mock(return_value=_captcha_image_response())
    respx.post(f"{BASE}/default.aspx").mock(return_value=_login_success_response())
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        cookie = await login(
            http=http, base_url=BASE, username="u", password="p",
            captcha_solver=FakeCaptchaSolver(answers=["34596"]), max_attempts=1,
            diag=rec,
        )
    assert cookie == "COOKIE_FIRST"
    steps = {s["name"]: s for s in rec.snapshot()["steps"]}
    assert steps["login.page"]["phase"] == "auth"
    assert steps["login.page"]["http"] is True
    assert steps["login.captcha_img"]["phase"] == "auth"
    assert steps["login.submit"]["phase"] == "auth"


@respx.mock
async def test_login_captcha_solve_step_is_external_and_not_http():
    respx.get(f"{BASE}/default.aspx").mock(return_value=_login_page_response())
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?12345").mock(return_value=_captcha_image_response())
    respx.post(f"{BASE}/default.aspx").mock(return_value=_login_success_response())
    rec = DiagnosticsRecorder()
    async with httpx.AsyncClient(base_url=BASE) as http:
        await login(
            http=http, base_url=BASE, username="u", password="p",
            captcha_solver=FakeCaptchaSolver(answers=["34596"]), max_attempts=1,
            diag=rec,
        )
    steps = {s["name"]: s for s in rec.snapshot()["steps"]}
    assert steps["login.captcha_solve"]["external"] is True
    assert steps["login.captcha_solve"]["http"] is False
