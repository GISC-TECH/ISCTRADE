"""Hedge-mode guard (one-way mode required for live trading)."""

import uuid

import pytest

from app.services import position_mode
from app.services.position_mode import HedgeModeUnsupported, ensure_one_way


class Client:
    def __init__(self, hedge: bool) -> None:
        self.hedge, self.calls = hedge, 0

    async def get_position_mode(self) -> bool:
        self.calls += 1
        return self.hedge


async def test_hedge_mode_refused_with_clear_message() -> None:
    with pytest.raises(HedgeModeUnsupported, match="one-way"):
        await ensure_one_way(Client(True), uuid.uuid4())


async def test_one_way_result_is_cached() -> None:
    acc, c = uuid.uuid4(), Client(False)
    await ensure_one_way(c, acc)
    await ensure_one_way(c, acc)
    assert c.calls == 1
    position_mode.forget(acc)
    await ensure_one_way(c, acc)
    assert c.calls == 2


async def test_hedge_result_is_not_cached() -> None:
    acc, c = uuid.uuid4(), Client(True)
    for _ in range(2):
        with pytest.raises(HedgeModeUnsupported):
            await ensure_one_way(c, acc)
    assert c.calls == 2  # switching the account to one-way mode takes effect immediately
