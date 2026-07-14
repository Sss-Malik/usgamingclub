import pytest

from app.backends.diagnostics import DiagnosticsRecorder
from app.preflight.checks import PreflightError, build_context


@pytest.mark.asyncio
async def test_game_resolved_by_name_credentials_mapped(seeded):
    async with seeded() as s:
        ctx = await build_context(
            s, type="READ_BALANCE", user_id=42,
            backend_name="milkyway", username="player_one",
        )
    assert ctx.credentials.name == "milkyway"
    assert ctx.credentials.login_page_url == "https://mw.test/default.aspx"
    assert ctx.credentials.backend_username == "TestMW159"
    assert ctx.credentials.backend_driver == "milkyway"
    assert ctx.credentials.backend_url == "https://mw.test/Cashier.aspx"


@pytest.mark.asyncio
async def test_account_scoped_context_loads_account(seeded):
    async with seeded() as s:
        ctx = await build_context(
            s, type="READ_BALANCE", user_id=42,
            backend_name="milkyway", username="player_one",
        )
    assert ctx.account is not None
    assert ctx.account.username == "player_one"
    assert ctx.account.external_user_id == "uid:gid"


@pytest.mark.asyncio
async def test_account_external_user_id_maps_from_id_from_backend(seeded):
    async with seeded() as s:
        ctx = await build_context(
            s, type="RECHARGE", user_id=43,
            backend_name="GameVault Demo", username="user020301",
        )
    assert ctx.account.external_user_id == "88880212"


@pytest.mark.asyncio
async def test_create_account_has_no_account(seeded):
    async with seeded() as s:
        ctx = await build_context(
            s, type="CREATE_ACCOUNT", user_id=42,
            backend_name="milkyway", username=None,
        )
    assert ctx.account is None
    assert ctx.user_id == 42


@pytest.mark.asyncio
async def test_create_account_username_flows_into_context(seeded):
    async with seeded() as s:
        ctx = await build_context(
            s, type="CREATE_ACCOUNT", user_id=43,
            backend_name="GameVault Demo", username=None,
            account_username="usr_43",
        )
    assert ctx.account_username == "usr_43"
    assert ctx.account is None


@pytest.mark.asyncio
async def test_missing_game_raises(seeded):
    async with seeded() as s:
        with pytest.raises(PreflightError) as ei:
            await build_context(
                s, type="AGENT_BALANCE", user_id=None,
                backend_name="nonexistent_game", username=None,
            )
    assert "game_not_found" in ei.value.reason


@pytest.mark.asyncio
async def test_recharge_with_unknown_username_raises(seeded):
    async with seeded() as s:
        with pytest.raises(PreflightError) as ei:
            await build_context(
                s, type="RECHARGE", user_id=42,
                backend_name="milkyway", username="ghost_user",
            )
    assert "game_account_not_found: ghost_user" in ei.value.reason


@pytest.mark.asyncio
async def test_recharge_with_no_username_raises(seeded):
    async with seeded() as s:
        with pytest.raises(PreflightError) as ei:
            await build_context(
                s, type="RECHARGE", user_id=42,
                backend_name="milkyway", username=None,
            )
    assert "missing_username" in ei.value.reason


@pytest.mark.asyncio
async def test_freeplay_is_account_scoped(seeded):
    async with seeded() as s:
        ctx = await build_context(
            s, type="FREEPLAY", user_id=42,
            backend_name="milkyway", username="player_one",
        )
    assert ctx.account is not None and ctx.account.username == "player_one"


@pytest.mark.asyncio
async def test_gamevault_missing_credentials_raises(seeded):
    async with seeded() as s:
        with pytest.raises(PreflightError) as ei:
            await build_context(
                s, type="AGENT_BALANCE", user_id=None,
                backend_name="GameVault NoCreds", username=None,
            )
    assert "missing_gamevault_credentials" in ei.value.reason


@pytest.mark.asyncio
async def test_gameroom_missing_credentials_raises(seeded):
    async with seeded() as s:
        with pytest.raises(PreflightError) as ei:
            await build_context(
                s, type="AGENT_BALANCE", user_id=None,
                backend_name="Gameroom NoCreds", username=None,
            )
    assert "missing_gameroom_credentials" in ei.value.reason


@pytest.mark.asyncio
async def test_goldentreasure_missing_credentials_raises(seeded):
    async with seeded() as s:
        with pytest.raises(PreflightError) as ei:
            await build_context(
                s, type="AGENT_BALANCE", user_id=None,
                backend_name="GT NoCreds", username=None,
            )
    assert "missing_goldentreasure_credentials" in ei.value.reason


@pytest.mark.asyncio
async def test_gameroom_context_carries_credentials(seeded):
    async with seeded() as s:
        ctx = await build_context(
            s, type="READ_BALANCE", idempotency_key="idem-1", user_id=51,
            backend_name="Gameroom", username="apifull9983654",
        )
    assert ctx.credentials.backend_driver == "gameroom"
    assert ctx.credentials.backend_url == "https://gr.test"
    assert ctx.credentials.backend_username == "TestGR159"
    assert ctx.account.external_user_id == "2998032"


@pytest.mark.asyncio
async def test_goldentreasure_context_carries_credentials(seeded):
    async with seeded() as s:
        ctx = await build_context(
            s, type="READ_BALANCE", idempotency_key="idem-1", user_id=61,
            backend_name="Golden Treasure", username="apitest01",
        )
    assert ctx.credentials.backend_driver == "goldentreasure"
    assert ctx.credentials.backend_url == "https://gt.test"
    assert ctx.credentials.backend_username == "Test02Gd1WEB"
    assert ctx.account.username == "apitest01"
    assert ctx.account.external_user_id is None


@pytest.mark.asyncio
async def test_context_carries_idempotency_key(seeded):
    async with seeded() as s:
        ctx = await build_context(
            s, type="READ_BALANCE", idempotency_key="idem-1", user_id=43,
            backend_name="GameVault Demo", username="user020301",
        )
    assert ctx.credentials.backend_driver == "gamevault"
    assert ctx.idempotency_key == "idem-1"


@pytest.mark.asyncio
async def test_build_context_attaches_recorder(seeded):
    rec = DiagnosticsRecorder()
    async with seeded() as s:
        ctx = await build_context(
            s, type="READ_BALANCE", user_id=42,
            backend_name="milkyway", username="player_one",
            diagnostics=rec, op_id="01J", attempt=2,
        )
    assert ctx.diag is rec
    assert ctx.op_id == "01J"
    assert ctx.attempt == 2


@pytest.mark.asyncio
async def test_yolo_missing_credentials(session_factory):
    from app.db.models import Game
    from app.preflight.checks import PreflightError, build_context
    async with session_factory() as s:
        s.add(Game(id=77, name="yolo", active=True, backend_driver="yolo"))
        await s.commit()
    async with session_factory() as s:
        with pytest.raises(PreflightError, match="missing_yolo_credentials"):
            await build_context(s, type="CREATE_ACCOUNT", backend_name="yolo",
                                username=None, user_id=2, account_username="abc123x")
