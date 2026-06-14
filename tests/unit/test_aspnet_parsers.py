import pytest

from app.backends._aspnet_cashier.parsers import (
    ViewState,
    parse_agent_balance_widget,
    parse_dialog_response,
    parse_get_score_response,
    parse_milkyway_balance_row,
    parse_sentinel,
    parse_update_select,
    parse_viewstate,
)

# --- viewstate ---

_VS_WITH_EVENTVALIDATION = """
<form id="form1">
  <input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="dDwxNDc=" />
  <input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="CA0B0334" />
  <input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="/wEdAAU=" />
</form>
"""

_VS_NO_EVENTVALIDATION = """
<form id="form1">
  <input type="hidden" name="__VIEWSTATE" value="dDwxNDc=" />
  <input type="hidden" name="__VIEWSTATEGENERATOR" value="CF7AEB79" />
</form>
"""


def test_viewstate_scrapes_all_three_hidden_fields():
    vs = parse_viewstate(_VS_WITH_EVENTVALIDATION)
    assert isinstance(vs, ViewState)
    assert vs.viewstate == "dDwxNDc="
    assert vs.viewstate_generator == "CA0B0334"
    assert vs.event_validation == "/wEdAAU="


def test_viewstate_eventvalidation_is_none_when_absent():
    vs = parse_viewstate(_VS_NO_EVENTVALIDATION)
    assert vs.viewstate == "dDwxNDc="
    assert vs.viewstate_generator == "CF7AEB79"
    assert vs.event_validation is None


def test_viewstate_raises_when_required_field_missing():
    with pytest.raises(ValueError, match="__VIEWSTATE"):
        parse_viewstate("<form></form>")


def test_viewstate_generator_is_none_when_absent():
    """Pandamaster's default.aspx and AccountsList.aspx omit __VIEWSTATEGENERATOR.
    The parser must tolerate this and return None rather than raising.
    Findings doc top-section callout: Pandamaster runs on 443 and omits VSG."""
    html = """
    <form id="form1">
      <input type="hidden" name="__VIEWSTATE" value="dDwxNDc=" />
      <input type="hidden" name="__SCROLLPOSITIONX" value="0" />
    </form>
    """
    vs = parse_viewstate(html)
    assert vs.viewstate == "dDwxNDc="
    assert vs.viewstate_generator is None
    assert vs.event_validation is None


def test_viewstate_still_raises_when_viewstate_itself_missing():
    """__VIEWSTATE remains mandatory — only the generator becomes optional."""
    with pytest.raises(ValueError, match="__VIEWSTATE"):
        parse_viewstate("<form><input type='hidden' name='__VIEWSTATEGENERATOR' value='G' /></form>")


# --- sentinel ---

def test_sentinel_success_recharge_redeem_includes_balance_arg():
    kind, args = parse_sentinel(
        'foo<script>showAlter("Confirmed successful","Balance:30");</script>bar'
    )
    assert kind == "success"
    assert args == ["Confirmed successful", "Balance:30"]


def test_sentinel_success_reset_password_single_arg():
    kind, args = parse_sentinel('<script>showAlter("Modified success!");</script>')
    assert kind == "success"
    assert args == ["Modified success!"]


def test_sentinel_success_create_account_uses_testAlter():
    kind, args = parse_sentinel('<script>testAlter("Added successfully");</script>')
    assert kind == "success"
    assert args == ["Added successfully"]


def test_sentinel_business_failure_insufficient_agent_funds():
    kind, args = parse_sentinel(
        '<script>showAlter("Sorry, the surplus money is insufficient!");</script>'
    )
    assert kind == "business_failure"
    assert args == ["Sorry, the surplus money is insufficient!"]


def test_sentinel_business_failure_insufficient_player_credit():
    kind, args = parse_sentinel(
        '<script>showAlter("Sorry, there is not enough gold for the operator!");</script>'
    )
    assert kind == "business_failure"
    assert args == ["Sorry, there is not enough gold for the operator!"]


def test_sentinel_business_failure_password_mismatch_via_alert():
    kind, args = parse_sentinel('<script>alert("Inconsistent passwords entered");</script>')
    assert kind == "business_failure"
    assert args == ["Inconsistent passwords entered"]


def test_sentinel_business_failure_create_errors_use_testAlter():
    for msg in [
        "The account number already exists, please re-enter it!",
        "entered passwords differ from the another.",
        "account name should be compose with letters letters, underscore & numbers.",
    ]:
        kind, args = parse_sentinel(f'<script>testAlter("{msg}");</script>')
        assert kind == "business_failure"
        assert args == [msg]


def test_sentinel_unknown_when_no_script_match():
    kind, args = parse_sentinel("<html><body>nothing here</body></html>")
    assert kind == "unknown"
    assert args == []


# --- updateSelect ---

def test_update_select_parses_first_row_uid_gid():
    html = """
    <table>
      <tr><td><a onclick="updateSelect( '21041615,21219386')">Update</a></td></tr>
      <tr><td><a onclick="updateSelect( '99999999,88888888')">Update</a></td></tr>
    </table>
    """
    pairs = parse_update_select(html)
    assert pairs == [("21041615", "21219386"), ("99999999", "88888888")]


def test_update_select_returns_empty_when_no_rows():
    assert parse_update_select("<table></table>") == []


# --- getscoreuserid response ---

def test_parse_get_score_response_returns_credit_and_totalwin():
    body = "0.00@0.00|<full AccountsList HTML...>"
    credit, totalwin = parse_get_score_response(body)
    assert credit == "0.00"
    assert totalwin == "0.00"


def test_parse_get_score_response_handles_nonzero_values():
    body = "1234.56@7890.12|<html>...</html>"
    credit, totalwin = parse_get_score_response(body)
    assert credit == "1234.56"
    assert totalwin == "7890.12"


def test_parse_get_score_response_raises_when_no_prefix():
    with pytest.raises(ValueError):
        parse_get_score_response("not a valid response")


# --- dialog (tourl) response ---

def test_parse_dialog_response_returns_url_and_param():
    body = (
        "Module/AccountManager/GrantTreasure.aspx?param=75517D2841C0311A6F33B18FBDC9A232DD313A7FF5BA019430EE38F7A28A2F15"
        "|<full html...>"
    )
    url, token = parse_dialog_response(body)
    assert url == "Module/AccountManager/GrantTreasure.aspx?param=75517D2841C0311A6F33B18FBDC9A232DD313A7FF5BA019430EE38F7A28A2F15"
    assert token == "75517D2841C0311A6F33B18FBDC9A232DD313A7FF5BA019430EE38F7A28A2F15"


def test_parse_dialog_response_raises_when_empty():
    with pytest.raises(ValueError, match="please_select_first"):
        parse_dialog_response("|<html>...</html>")


# --- agent balance widget ---

def test_parse_agent_balance_widget_extracts_first_balance_int():
    html = '<div class="navTop">Balance:31</div>... other Balance:99 elsewhere'
    assert parse_agent_balance_widget(html) == 31


def test_parse_agent_balance_widget_raises_when_missing():
    with pytest.raises(ValueError, match="agent_balance_widget"):
        parse_agent_balance_widget("<html>no balance widget</html>")


# --- milkyway balance row ---

_MW_ROW_HTML = """
<table>
  <tr>
    <td><a onclick="updateSelect( '21041615,21219386')">Update</a></td>
    <td>21219386</td>
    <td>Saud_Doe892</td>
    <td>Saud</td>
    <td>123.45</td>
    <td>2026-05-30</td>
    <td>2026-06-01</td>
    <td>TestMW159</td>
    <td>Active</td>
  </tr>
</table>
"""


def test_milkyway_balance_row_extracts_balance_for_matching_account():
    bal = parse_milkyway_balance_row(_MW_ROW_HTML, account="Saud_Doe892")
    assert bal == "123.45"


def test_milkyway_balance_row_matches_by_gameid_when_account_misses():
    bal = parse_milkyway_balance_row(_MW_ROW_HTML, account="21219386")
    assert bal == "123.45"


def test_milkyway_balance_row_raises_when_no_matching_row():
    with pytest.raises(ValueError, match="row_not_found"):
        parse_milkyway_balance_row(_MW_ROW_HTML, account="other_account")
