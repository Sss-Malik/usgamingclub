from app.postflight.effects import apply_post_effects


async def test_apply_post_effects_is_noop_and_returns_none():
    result = await apply_post_effects("idem-key", "READ_BALANCE", {"balance": 1})
    assert result is None
