import httpx
import pytest
import respx

from app.backends.base import BackendError
from app.backends._aspnet_cashier.login import login
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
