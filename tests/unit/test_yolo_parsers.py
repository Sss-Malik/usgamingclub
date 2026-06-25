import pytest

from app.backends.base import BackendError, TransientBackendError
from app.backends.yolo.parsers import (
    looks_like_login_page,
    parse_agent_score,
    parse_csrf_token,
    parse_player_row,
)

# Realistic single-row player_list grid fragment. Column order per findings §3:
# Action | Player ID | Account | nickname | AgentAccount | KindName | Player Score | ...
_GRID = """
<table><tbody>
<tr>
  <td><a href="#">edit</a></td>
  <td>922952</td>
  <td><span data-content="apitest102"></span>&nbsp;apitest102</td>
  <td>nick102</td>
  <td>webyolo1</td>
  <td>Member</td>
  <td>123.45</td>
  <td>1000</td><td>900</td><td>5</td><td>3</td><td>2</td>
  <td>Normal</td><td>0.0.0.0</td><td>2026-01-01</td><td>2026-06-01</td>
</tr>
</tbody></table>
"""

_LOGIN_PAGE = """
<html><body><form action="/admin/auth/login" method="post">
<input name="username"><input name="password" type="password">
<script>window.Dcat = {token: "LOGIN_TOK_123"}; Dcat.token = "LOGIN_TOK_123";</script>
</form></body></html>
"""


def test_parse_agent_score():
    assert parse_agent_score("10.00\n") == 10.0
    assert parse_agent_score("  7 ") == 7.0


def test_parse_player_row_match():
    uid, score = parse_player_row(_GRID, account="apitest102")
    assert uid == "922952" and score == 123.45


def test_parse_player_row_no_match_raises():
    with pytest.raises(BackendError, match="player_not_found"):
        parse_player_row(_GRID, account="someone_else")


def test_parse_csrf_token():
    assert parse_csrf_token('foo Dcat.token = "ABC123" bar') == "ABC123"


def test_parse_csrf_token_missing_raises():
    with pytest.raises(TransientBackendError, match="csrf_token_not_found"):
        parse_csrf_token("<html>no token here</html>")


def test_looks_like_login_page():
    assert looks_like_login_page(_LOGIN_PAGE) is True
    assert looks_like_login_page(_GRID) is False
