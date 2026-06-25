import re

from app.backends.base import BackendError, TransientBackendError

_CSRF_RE = re.compile(r'Dcat\.token\s*=\s*"([^"]+)"')
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")

# Column indices in the player_list grid (findings §3). 0 = Action.
_COL_PLAYER_ID = 1
_COL_ACCOUNT = 2
_COL_SCORE = 6


def _strip(cell: str) -> str:
    text = _TAG_RE.sub("", cell)
    return text.replace("&nbsp;", " ").strip()


def parse_agent_score(text: str) -> float:
    """`GET /admin/refresh_score` returns the agent balance as a bare number string."""
    return float(text.strip())


def parse_player_row(html: str, *, account: str) -> tuple[str, float]:
    """Find the grid row whose Account column matches `account`; return (player_id, player_score).

    Account cells render as `<span data-content="acct"></span>&nbsp;acct`; we compare the
    visible (tag-stripped) text. Raises BackendError if no row matches.
    """
    for row in _ROW_RE.finditer(html):
        tds = [_strip(m.group(1)) for m in _TD_RE.finditer(row.group(1))]
        if len(tds) <= _COL_SCORE:
            continue
        if tds[_COL_ACCOUNT] == account:
            try:
                score = float(tds[_COL_SCORE])
            except ValueError as exc:
                raise BackendError("yolo:player_score_unparseable") from exc
            return tds[_COL_PLAYER_ID], score
    raise BackendError("yolo:player_not_found")


def parse_csrf_token(html: str) -> str:
    """Scrape the per-session Dcat CSRF token from an admin page."""
    m = _CSRF_RE.search(html)
    if not m:
        raise TransientBackendError("yolo:csrf_token_not_found")
    return m.group(1)


def looks_like_login_page(text: str) -> bool:
    """Heuristic: an unauthenticated response renders the admin login form."""
    return "/admin/auth/login" in text and 'name="password"' in text
