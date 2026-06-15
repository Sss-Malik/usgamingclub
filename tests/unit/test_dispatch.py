import pytest

from app.operations.dispatch import dispatch
from app.schemas.requests import Operation


class _Spy:
    def __init__(self):
        self.calls = []

    async def recharge(self, ctx, *, amount):
        self.calls.append(("recharge", amount))
        return _R()

    async def redeem(self, ctx, *, amount):
        self.calls.append(("redeem", amount))
        return _R()

    async def read_balance(self, ctx):
        self.calls.append(("read", None))
        return _R()


class _R:
    def model_dump(self, **k):
        return {}


def _op(type_, amount=None):
    return Operation(action="recharge", type=type_, idempotency_key="k",
                     user_id=1, backend_name="x", amount=amount)


@pytest.mark.asyncio
async def test_freeplay_maps_to_recharge():
    spy = _Spy()
    await dispatch(spy, _op("FREEPLAY", 50), ctx=None)
    assert spy.calls == [("recharge", 50)]


@pytest.mark.asyncio
async def test_recharge_passes_dollars():
    spy = _Spy()
    await dispatch(spy, _op("RECHARGE", 25), ctx=None)
    assert spy.calls == [("recharge", 25)]
