# tests/unit/test_yolo_backend.py
import pytest

from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.yolo.backend import YoloBackend


class FakeClient:
    """Stand-in for YoloClient: canned get_text + records post_form calls."""

    def __init__(self, *, texts=None, post_result=None):
        self._texts = texts or {}
        self._post_result = post_result if post_result is not None else {"message": "success"}
        self.posts = []

    async def get_text(self, path, params=None):
        for key, val in self._texts.items():
            if key in path:
                # crude param-aware: store last params for assertions
                self.last_params = params
                return val
        raise AssertionError(f"unexpected get_text {path}")

    async def post_form(self, path, fields):
        self.posts.append((path, fields))
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


def _ctx(*, account_username=None, username="apitest102", external_user_id=None):
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
                          idempotency_key="k", account_username=account_username)


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
