from decimal import Decimal

import pytest

from app.models.enums import OrderSide
from app.strategy.base import StrategyContext, StrategyParamError
from app.strategy.grid import GridStrategy, grid_levels
from app.strategy.strategies import build_strategy, list_strategies
from tests.factories import candles_from_closes

GRID = {"lower_price": 90, "upper_price": 110, "levels": 5, "quantity_per_level": 0.1, "stop_loss_pct": 5}


def ctx(closes, qty="0", state=None, equity="10000"):
    return StrategyContext(
        symbol="BTCUSDT",
        candles=candles_from_closes(closes),
        position_qty=Decimal(qty),
        position_entry=Decimal("0"),
        equity=Decimal(equity),
        leverage=1,
        params={},
        state=state if state is not None else {},
    )


def test_registry_lists_strategies() -> None:
    keys = {s["key"] for s in list_strategies()}
    assert {"grid", "ema_cross", "rsi_bollinger"} <= keys


def test_param_validation() -> None:
    with pytest.raises(StrategyParamError):
        GridStrategy({"lower_price": 100, "upper_price": 90, "quantity_per_level": 1})
    with pytest.raises(StrategyParamError):
        GridStrategy({**GRID, "bogus": 1})
    with pytest.raises(StrategyParamError):
        build_strategy("ema_cross", {"fast": 30, "slow": 10})
    with pytest.raises(StrategyParamError):
        build_strategy("nope")


def test_grid_levels() -> None:
    assert grid_levels(Decimal(90), Decimal(110), 5) == [90, 95, 100, 105, 110]


def test_grid_buys_on_down_cross_and_sells_next_level() -> None:
    s = GridStrategy(GRID)
    state: dict = {}
    assert s.on_candle(ctx([101], state=state)) == []  # first tick only records price
    c = ctx([101, 99], state=state)
    intents = s.on_candle(c)
    # levels: 90, 95, 100, 105, 110 -> falling from 101 to 99 crosses 100 (index 2)
    assert [(i.side, i.meta["level"]) for i in intents] == [(OrderSide.BUY, 2)]
    assert intents[0].stop_loss == Decimal("85.5")
    s.on_result(intents[0], True, c)
    assert state["held"] == [intents[0].meta["level"]]


def test_grid_full_cycle_and_stop() -> None:
    s = GridStrategy(GRID)
    state: dict = {}
    s.on_candle(ctx([102], state=state))
    c = ctx([102, 99], state=state)
    buys = s.on_candle(c)
    for i in buys:
        s.on_result(i, True, c)
    lvl = buys[0].meta["level"]
    c2 = ctx([99, 106], qty="0.1", state=state)
    sells = s.on_candle(c2)
    assert len(sells) == 1 and sells[0].side == OrderSide.SELL and sells[0].reduce_only
    s.on_result(sells[0], True, c2)
    assert lvl not in state["held"]
    # buy again then crash below stop
    c3 = ctx([106, 99], state=state)
    for i in s.on_candle(c3):
        s.on_result(i, True, c3)
    c4 = ctx([99, 80], qty="0.2", state=state)
    stop = s.on_candle(c4)
    assert len(stop) == 1 and stop[0].meta["action"] == "stop"
    s.on_result(stop[0], True, c4)
    assert state["halted"] and state["held"] == []
    assert s.on_candle(ctx([80, 120], state=state)) == []


def test_grid_rejected_order_does_not_mark_level() -> None:
    s = GridStrategy(GRID)
    state: dict = {}
    s.on_candle(ctx([101], state=state))
    c = ctx([101, 99], state=state)
    for i in s.on_candle(c):
        s.on_result(i, False, c)
    assert state["held"] == []


def test_ema_cross_long_then_reverse() -> None:
    s = build_strategy("ema_cross", {"fast": 3, "slow": 6, "atr_period": 3, "atr_multiplier": 2})
    down = [100 - i for i in range(15)]
    closes = down + [86, 90, 95]
    # find first candle where a long signal appears
    long_intents = None
    for n in range(10, len(closes) + 1):
        out = s.on_candle(ctx(closes[:n]))
        if out:
            long_intents = out
            break
    assert long_intents is not None
    entry = long_intents[-1]
    assert entry.side == OrderSide.BUY and entry.quantity is None
    assert entry.stop_loss is not None and entry.stop_loss < Decimal(str(closes[n - 1]))

    up = [80 + i for i in range(15)]
    closes2 = up + [90, 85, 80]
    for n in range(10, len(closes2) + 1):
        out = s.on_candle(ctx(closes2[:n], qty="1"))
        if out:
            break
    assert out[0].side == OrderSide.SELL and out[0].reduce_only and out[0].quantity == Decimal("1")
    assert out[1].side == OrderSide.SELL and out[1].stop_loss > Decimal(str(closes2[n - 1]))


def test_ema_cross_respects_allow_short() -> None:
    s = build_strategy("ema_cross", {"fast": 3, "slow": 6, "atr_period": 3, "allow_short": False})
    closes = [80 + i for i in range(15)] + [90, 85, 80]
    for n in range(10, len(closes) + 1):
        assert all(not (i.side == OrderSide.SELL and not i.reduce_only) for i in s.on_candle(ctx(closes[:n])))


def test_rsi_bollinger_entry() -> None:
    s = build_strategy("rsi_bollinger", {"rsi_period": 5, "bb_period": 10, "oversold": 30})
    closes = [100.0] * 20 + [99, 97, 94, 90, 85]
    out = s.on_candle(ctx(closes))
    assert out and out[0].side == OrderSide.BUY and out[0].stop_loss < Decimal("85")
