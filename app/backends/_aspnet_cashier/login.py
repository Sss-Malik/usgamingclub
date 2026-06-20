import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

from app.backends.base import BackendError, TransientBackendError
from app.backends._aspnet_cashier.errors import login_errtype_to_code
from app.backends._aspnet_cashier.parsers import parse_viewstate
from app.captcha.base import CaptchaSolver

_FORM_CT = "application/x-www-form-urlencoded; charset=UTF-8"

# Browser-flavored headers. `Accept-Language` is mandatory — without it the ASP.NET
# InitializeCulture() throws NRE and returns a 500 yellow-screen (findings §3).
_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_CAPTCHA_IMG_RE = re.compile(
    r'<img[^>]+src=["\']([^"\']*Tools/VerifyImagePage\.aspx\?[^"\']+)["\']',
    re.IGNORECASE,
)
_SESSION_COOKIE = "ASP.NET_SessionId"


async def login(
    *, http: httpx.AsyncClient, base_url: str,
    username: str, password: str,
    captcha_solver: CaptchaSolver,
    max_attempts: int = 3,
    driver_prefix: str = "aspnet",
) -> str:
    """Captcha-aware login. Returns the `ASP.NET_SessionId` cookie value.

    On `errtype=verifycode` we restart the attempt from a fresh GET (viewstate and captcha
    are both single-use and session-rotated). Any other `errtype` is terminal — we map it
    to a short code via `login_errtype_to_code()` and raise `BackendError`.

    `driver_prefix` is used in raised error codes ("orionstars" or "milkyway") so logs can
    distinguish the portal.
    """
    base = base_url.rstrip("/")
    last_errtype: str | None = None
    for _attempt in range(max_attempts):
        # The captcha is session-bound. Each retry must start with a clean cookie jar
        # so the server issues a brand-new ASP.NET_SessionId on the GET below.
        http.cookies.clear()
        # Each attempt uses a fresh cookie jar (cookies live on the http client only for the
        # duration of the attempt; on failure we forget them and start over).
        cookies: dict[str, str] = {}

        # 1. GET the login page.
        try:
            r1 = await http.get(f"{base}/default.aspx", headers=_BASE_HEADERS, cookies=cookies)
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"{driver_prefix}:login_transport:{type(exc).__name__}") from exc
        if r1.status_code >= 500:
            raise TransientBackendError(f"{driver_prefix}:login_http_{r1.status_code}")
        cookies.update({k: v for k, v in r1.cookies.items()})
        vs = parse_viewstate(r1.text)
        m = _CAPTCHA_IMG_RE.search(r1.text)
        if not m:
            raise TransientBackendError(f"{driver_prefix}:login_no_captcha_img")
        captcha_url = urljoin(f"{base}/", m.group(1))

        # 2. GET the captcha image (must reuse the same cookie jar).
        try:
            r2 = await http.get(captcha_url, headers=_BASE_HEADERS, cookies=cookies)
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"{driver_prefix}:login_transport:{type(exc).__name__}") from exc
        if r2.status_code != 200:
            raise TransientBackendError(f"{driver_prefix}:captcha_http_{r2.status_code}")
        cookies.update({k: v for k, v in r2.cookies.items()})

        # 3. Solve.
        try:
            text = await captcha_solver.solve_numeric_image(r2.content)
        except (BackendError, TransientBackendError):
            raise
        except Exception as exc:  # noqa: BLE001 - wrap unexpected solver errors
            raise TransientBackendError(
                f"{driver_prefix}:captcha_solver:{type(exc).__name__}"
            ) from exc

        # 4. POST credentials.
        form_fields = {
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "__LASTFOCUS": "",
            "__VIEWSTATE": vs.viewstate,
            "__VIEWSTATEGENERATOR": vs.viewstate_generator,
            "__EVENTVALIDATION": vs.event_validation or "",
            "ddlRole": "0",
            "txtLoginName": username,
            "txtLoginPass": password,
            "txtVerifyCode": text,
            "btnLogin": "Login in",
        }
        body = urlencode(form_fields).encode()
        try:
            r3 = await http.post(
                f"{base}/default.aspx", content=body,
                headers={**_BASE_HEADERS, "Content-Type": _FORM_CT},
                cookies=cookies, follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"{driver_prefix}:login_transport:{type(exc).__name__}") from exc
        if r3.status_code >= 500:
            raise TransientBackendError(f"{driver_prefix}:login_http_{r3.status_code}")
        if r3.status_code not in (301, 302):
            raise TransientBackendError(f"{driver_prefix}:login_unexpected_{r3.status_code}")

        loc = r3.headers.get("Location", "")
        qs = parse_qs(urlparse(loc).query)
        errtype = (qs.get("errtype") or [""])[0]
        landing = urlparse(loc).path.rsplit("/", 1)[-1].lower()

        # Success: the portal redirects an authenticated session to a landing page —
        # Cashier.aspx on most portals, Store.aspx on OrionStars. Every failure (bad creds,
        # wrong captcha, server hiccup) instead redirects back to default.aspx, with an
        # ?errtype=… marker for known errors or bare on a transient hiccup.
        if loc and landing not in ("", "default.aspx") and not errtype:
            # The ASP.NET_SessionId cookie in our jar is the authenticated one.
            cookie_val = cookies.get(_SESSION_COOKIE)
            if not cookie_val:
                raise TransientBackendError(f"{driver_prefix}:login_no_session_cookie")
            return cookie_val

        # Failure redirect — classify by `errtype` from the query string.
        last_errtype = errtype
        if errtype == "verifycode":
            continue                                # captcha wrong — restart attempt with fresh GET
        # Terminal: bad creds, banned IP, banned account, etc.
        code = login_errtype_to_code(errtype)
        if code.startswith("unknown:"):
            raise TransientBackendError(
                f"{driver_prefix}:login_failed_unmapped_errtype:{errtype!r}"
            )
        raise BackendError(f"{driver_prefix}:login_failed:{code}")

    # Exhausted attempts (only reachable via repeated `verifycode`).
    raise BackendError(
        f"{driver_prefix}:captcha_failed_max_attempts (last_errtype={last_errtype})"
    )
