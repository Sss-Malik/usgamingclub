# tests/unit/test_yolo_backend.py
import pytest

from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.diagnostics import DiagnosticsRecorder
from app.backends.yolo.backend import YoloBackend


class FakeClient:
    """Stand-in for YoloClient: canned get_text + records post_form calls.

    Accepts (and records) the `step`/`phase` kwargs the real YoloClient.post_form/get_text
    take, so tests can assert which step name backend.py passed for a given op, without
    needing the full respx + real-client harness (that's covered in test_yolo_client.py).
    """

    def __init__(self, *, texts=None, post_result=None):
        self._texts = texts or {}
        self._post_result = post_result if post_result is not None else {"message": "success"}
        self.posts = []        # (path, fields) -- unchanged shape for existing assertions
        self.post_steps = []   # step name per post_form call, same order as `posts`
        self.get_steps = []    # step name per get_text call

    async def get_text(self, path, params=None, *, step="primary", phase="primary"):
        self.get_steps.append(step)
        for key, val in self._texts.items():
            if key in path:
                # crude param-aware: store last params for assertions
                self.last_params = params
                return val
        raise AssertionError(f"unexpected get_text {path}")

    async def post_form(self, path, fields, *, step="primary", phase="primary"):
        self.posts.append((path, fields))
        self.post_steps.append(step)
        return self._post_result


# Real grid layout: [0]=Player ID, [1]=Account, [2]=NickName, [3]=AgentAccount,
# [4]=Status, [5]=Player Score, …, Action column LAST.
_GRID = """
<table><tbody><tr>
<td>922952</td><td>apitest102</td><td>nick</td><td>ag</td><td>Not online</td><td>123.45</td>
<td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td></td>
<td>0.0.0.0</td><td>d</td><td>d</td><td>Recharge Redeem Reset Password</td>
</tr></tbody></table>
"""


def _ctx(*, account_username=None, username="apitest102", external_user_id=None, diagnostics=None):
    creds = GameCredentials(
        game_id=1, name="yolo", backend_url="https://yolo.test", login_page_url=None,
        backend_username="webyolo1", backend_password="Web@@1122",
        api_base_url=None, api_agent_id=None, api_secret_key=None, binding_key=None,
        backend_driver="yolo",
    )
    account = None
    if username:
        account = AccountIdentity(game_account_id=1, user_id=2, game_id=1,
                                  username=username, external_user_id=external_user_id)
    return BackendContext(credentials=creds, user_id=2, account=account,
                          idempotency_key="k", account_username=account_username,
                          diagnostics=diagnostics)


async def test_agent_balance():
    c = FakeClient(texts={"/admin/refresh_score": "10.00"})
    res = await YoloBackend(c).agent_balance(_ctx())
    assert res.agent_balance == 10.0


async def test_read_balance_by_search():
    c = FakeClient(texts={"/admin/player_list": _GRID})
    res = await YoloBackend(c).read_balance(_ctx(username="apitest102"))
    assert res.balance == 123.45


async def test_recharge_sends_type1_int_dollars():
    c = FakeClient(texts={"/admin/player_list": _GRID})
    await YoloBackend(c).recharge(_ctx(external_user_id="922952"), amount=50)
    path, fields = c.posts[-1]
    assert path == "/admin/dcat-api/form"
    assert fields["type"] == 1 and fields["input_score"] == "50" and fields["UserID"] == "922952"
    assert fields["_form_"] == "App\\Admin\\Actions\\UserRecharge"


async def test_redeem_sends_type2():
    c = FakeClient(texts={"/admin/player_list": _GRID})
    await YoloBackend(c).redeem(_ctx(external_user_id="922952"), amount=25)
    _path, fields = c.posts[-1]
    assert fields["type"] == 2 and fields["input_score"] == "25"


async def test_reset_password_returns_generated_pw():
    c = FakeClient(texts={"/admin/player_list": _GRID}, post_result={"message": "success"})
    res = await YoloBackend(c).reset_password(_ctx(external_user_id="922952"))
    assert len(res.password) >= 6 and res.password.isalnum()
    _path, fields = c.posts[-1]
    assert fields["_form_"] == "App\\Admin\\Actions\\ResetUserPass"
    assert fields["password"] == res.password


async def test_create_account_generates_and_searches():
    c = FakeClient(texts={"/admin/player_list": _GRID},
                   post_result={"message": "<div>Account: x Password: y</div>"})
    res = await YoloBackend(c).create_account(_ctx(account_username="apitest102", username=None))
    assert res.username == "apitest102" and len(res.password) >= 6
    # external_user_id resolved from the follow-up player_list search
    assert res.external_user_id == "922952"
    create_path, fields = c.posts[0]
    assert create_path == "/admin/player_list"
    assert fields["Accounts"] == "apitest102" and fields["RegisterIP"] == "0.0.0.0"
    # The AccountsInfo INSERT has columns with no DB default (e.g. LastLogonIP), so the
    # full hidden-field set the browser submits must be sent or it fails with SQLSTATE 1364.
    assert fields["LastLogonIP"] == "0.0.0.0"
    for hidden in ("ChannelID", "RegAccounts", "AgentID", "InsurePass", "FaceID",
                   "MemberOrder", "MemberExp", "RegisterMobile", "RegisterMachine",
                   "BindAgentDate", "Nullity"):
        assert hidden in fields


async def test_recharge_resolves_player_id_via_search_when_uncached():
    c = FakeClient(texts={"/admin/player_list": _GRID})
    await YoloBackend(c).recharge(_ctx(username="apitest102", external_user_id=None), amount=5)
    _path, fields = c.posts[-1]
    assert fields["UserID"] == "922952"  # came from parse_player_row


async def test_create_account_requires_username():
    from app.backends.base import BackendError
    c = FakeClient(texts={"/admin/player_list": _GRID})
    with pytest.raises(BackendError, match="account_username_required"):
        await YoloBackend(c).create_account(_ctx(account_username=None, username=None))
    assert c.posts == []  # never hit the network


# ---- diagnostics: op step names + external_user_id marks (Appendix A) ----

async def test_recharge_records_recharge_post_step_and_marks_cached_external_user_id():
    c = FakeClient(texts={"/admin/player_list": _GRID})
    rec = DiagnosticsRecorder()
    await YoloBackend(c).recharge(_ctx(external_user_id="922952", diagnostics=rec), amount=50)
    assert c.post_steps[-1] == "recharge.post"
    assert c.get_steps == []  # cached external_user_id -> no search needed
    assert rec.snapshot()["external_user_id"] == "922952"


async def test_recharge_records_resolve_search_step_and_marks_resolved_external_user_id():
    c = FakeClient(texts={"/admin/player_list": _GRID})
    rec = DiagnosticsRecorder()
    await YoloBackend(c).recharge(
        _ctx(username="apitest102", external_user_id=None, diagnostics=rec), amount=5)
    assert "resolve.search" in c.get_steps
    assert c.post_steps[-1] == "recharge.post"
    assert rec.snapshot()["external_user_id"] == "922952"  # grid column 0


async def test_redeem_records_redeem_post_step():
    c = FakeClient(texts={"/admin/player_list": _GRID})
    rec = DiagnosticsRecorder()
    await YoloBackend(c).redeem(_ctx(external_user_id="922952", diagnostics=rec), amount=25)
    assert c.post_steps[-1] == "redeem.post"


async def test_reset_password_records_reset_post_step():
    c = FakeClient(texts={"/admin/player_list": _GRID})
    rec = DiagnosticsRecorder()
    await YoloBackend(c).reset_password(_ctx(external_user_id="922952", diagnostics=rec))
    assert c.post_steps[-1] == "reset.post"


async def test_create_account_records_create_post_step_and_marks_id_from_followup_search():
    c = FakeClient(texts={"/admin/player_list": _GRID},
                   post_result={"message": "<div>Account: x Password: y</div>"})
    rec = DiagnosticsRecorder()
    res = await YoloBackend(c).create_account(
        _ctx(account_username="apitest102", username=None, diagnostics=rec))
    assert c.post_steps[0] == "create.post"
    assert "resolve.search" in c.get_steps
    assert rec.snapshot()["external_user_id"] == "922952"
    assert res.external_user_id == "922952"


async def test_read_balance_records_balance_read_step_and_marks_external_user_id():
    c = FakeClient(texts={"/admin/player_list": _GRID})
    rec = DiagnosticsRecorder()
    res = await YoloBackend(c).read_balance(_ctx(username="apitest102", diagnostics=rec))
    assert res.balance == 123.45
    assert c.get_steps == ["balance.read"]      # NOT resolve.search -- this is the read-path get_text
    assert rec.snapshot()["external_user_id"] == "922952"


# ---- diagnostics: NO balance_after mark for yolo money ops (Appendix A flag) ----
# yolo's post_form success envelope is a Dcat {status,message} dict with no balance field, so
# there is nothing honest to retain into balance_after (read balances still flow via user_data).

async def test_recharge_does_not_mark_balance_after():
    c = FakeClient(texts={"/admin/player_list": _GRID}, post_result={"message": "success"})
    rec = DiagnosticsRecorder()
    await YoloBackend(c).recharge(_ctx(external_user_id="922952", diagnostics=rec), amount=50)
    assert rec.snapshot()["balance_after"] is None


async def test_redeem_does_not_mark_balance_after():
    c = FakeClient(texts={"/admin/player_list": _GRID}, post_result={"message": "success"})
    rec = DiagnosticsRecorder()
    await YoloBackend(c).redeem(_ctx(external_user_id="922952", diagnostics=rec), amount=25)
    assert rec.snapshot()["balance_after"] is None
