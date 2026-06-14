import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ViewState:
    viewstate: str
    viewstate_generator: str | None  # Pandamaster omits this on default.aspx + AccountsList.aspx
    event_validation: str | None     # None on pages with EnableEventValidation="false" (AccountsList.aspx)


_HIDDEN_RE = re.compile(
    r'<input[^>]*name=["\'](?P<name>__[A-Z]+)["\'][^>]*value=["\'](?P<value>[^"\']*)["\']',
    re.IGNORECASE,
)


def parse_viewstate(html: str) -> ViewState:
    """Scrape ASP.NET hidden fields from a rendered form.

    __VIEWSTATE is required (no portal ships without it). __VIEWSTATEGENERATOR is optional —
    Pandamaster omits it on default.aspx and AccountsList.aspx; per findings §3 we must "send
    exactly the hidden fields the GET actually contained", and the ctl16 search postback works
    without it. __EVENTVALIDATION is page-specific: dialog pages (GrantTreasure, ChangeTreasure,
    ResetPassWord, CreateAccount) include it; AccountsList.aspx does not.
    """
    fields: dict[str, str] = {}
    for m in _HIDDEN_RE.finditer(html):
        fields[m.group("name").upper()] = m.group("value")
    if "__VIEWSTATE" not in fields:
        raise ValueError("__VIEWSTATE not found in form HTML")
    return ViewState(
        viewstate=fields["__VIEWSTATE"],
        viewstate_generator=fields.get("__VIEWSTATEGENERATOR"),
        event_validation=fields.get("__EVENTVALIDATION"),
    )


# Captures the trailing inline-script sentinel that ASP.NET cashier responses use to signal
# success/failure. Accepts showAlter / testAlter / alert with 1 or 2 string args.
_SENTINEL_RE = re.compile(
    r'(?:showAlter|testAlter|alert)\(\s*"([^"]*)"(?:\s*,\s*"([^"]*)")?\s*\)',
)

# Sentinel messages we know are terminal business failures (the rest of "success" sentinels
# are pinned by exact string in the per-op response handlers). For the parser layer, we only
# need to distinguish "success" (the known-success strings) from "business_failure" (everything
# else that matched a script) from "unknown" (no script match).
_KNOWN_SUCCESS_MESSAGES = frozenset({
    "Confirmed successful",
    "Modified success!",
    "Added successfully",
})


def parse_sentinel(html: str) -> tuple[str, list[str]]:
    """Pattern-match the trailing inline-script sentinel.

    Returns: (kind, args)
      - kind="success",          args=[message, ...extras]   for known-success messages
      - kind="business_failure", args=[message, ...extras]   for any other matched message
      - kind="unknown",          args=[]                     when no sentinel script matched
    """
    m = _SENTINEL_RE.search(html)
    if not m:
        return ("unknown", [])
    args = [m.group(1)] + ([m.group(2)] if m.group(2) is not None else [])
    kind = "success" if args[0] in _KNOWN_SUCCESS_MESSAGES else "business_failure"
    return (kind, args)


_UPDATE_SELECT_RE = re.compile(
    r"updateSelect\(\s*'(?P<uid>\d+)\s*,\s*(?P<gid>\d+)'\s*\)"
)


def parse_update_select(html: str) -> list[tuple[str, str]]:
    """Extract every (UserID, GameID) pair from `updateSelect('<uid>,<gid>')` JS handlers."""
    return [(m.group("uid"), m.group("gid")) for m in _UPDATE_SELECT_RE.finditer(html)]


def parse_get_score_response(body: str) -> tuple[str, str]:
    """Parse `<credit>@<totalwin>|<html...>` from OrionStars `getscoreuserid` POST.

    Returns the two leading string values (we keep them as strings; caller converts to cents).
    """
    if "|" not in body or "@" not in body.split("|", 1)[0]:
        raise ValueError("getscoreuserid response has no `credit@totalwin|` prefix")
    head = body.split("|", 1)[0]
    credit, totalwin = head.split("@", 1)
    return (credit, totalwin)


def parse_dialog_response(body: str) -> tuple[str, str]:
    """Parse the `<dialogURL>?param=<TOKEN>|<html...>` reply from the `tourl` POST.

    Returns (dialog_url, param_token). Raises ValueError if the URL is empty
    (server returns just `|...` when no player is selected).
    """
    head = body.split("|", 1)[0]
    if not head:
        raise ValueError("aspnet:please_select_first")
    if "param=" not in head:
        raise ValueError("aspnet:dialog_url_missing_param")
    token = head.rsplit("param=", 1)[-1]
    return (head, token)


_BALANCE_WIDGET_RE = re.compile(r"Balance\s*:\s*(\d+)")


def parse_agent_balance_widget(html: str) -> int:
    """Extract the agent's `Balance:NN` (integer dollars) from the page chrome."""
    m = _BALANCE_WIDGET_RE.search(html)
    if not m:
        raise ValueError("aspnet:agent_balance_widget_not_found")
    return int(m.group(1))


# Matches a <tr>...</tr> block; we walk its <td>s in order.
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(s: str) -> str:
    return _TAG_RE.sub("", s).strip()


def parse_milkyway_balance_row(html: str, *, account: str) -> str:
    """MilkyWay-specific: locate the row whose Account (td[2]) or GameID (td[1]) matches,
    then return Balance/Credit from td[4]. See findings §4.1 portal-difference note.
    """
    for row_match in _ROW_RE.finditer(html):
        tds = [_strip_tags(m.group(1)) for m in _TD_RE.finditer(row_match.group(1))]
        if len(tds) < 5:
            continue
        # td[1] = GameID, td[2] = Account, td[4] = Balance/Credit
        if tds[1] == account or tds[2] == account:
            return tds[4]
    raise ValueError("aspnet:milkyway_balance_row_not_found")
