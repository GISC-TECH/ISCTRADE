"""New technical strategies: indicators, entry/exit signals with ATR stops, and backtest smoke tests."""

import math
from decimal import Decimal

import pytest

from app.backtest.engine import run_backtest
from app.models.enums import OrderSide
from app.strategy.base import StrategyContext, StrategyParamError
from app.strategy.indicators import adx, donchian, highest, lowest, supertrend
from app.strategy.strategies import STRATEGIES, build_strategy, entry_signal, list_strategies
from app.strategy.technical import TECHNICAL_STRATEGIES
from tests.factories import candles_from_closes, sine_closes

NEW_KEYS = {
    "rsi_mean_reversion",
    "bollinger_reversion",
    "bollinger_squeeze",
    "donchian_breakout",
    "ema_adx_trend",
    "supertrend",
}


def ctx(closes, qty="0", state=None, entry="0", spread=0.001):
    return StrategyContext(
        symbol="BTCUSDT",
        candles=candles_from_closes(closes, spread=spread),
        position_qty=Decimal(qty),
        position_entry=Decimal(entry),
        equity=Decimal("10000"),
        leverage=1,
        params={},
        state=state if state is not None else {},
    )


def assert_atr_entry(intents, side):
    entries = [i for i in intents if not i.reduce_only]
    assert entries, intents
    e = entries[-1]
    assert e.side == side and e.stop_loss is not None and e.quantity is None
    assert e.meta["atr"] > 0
    return e


# ------------------------------------------------------------------ indicators
def test_highest_lowest_donchian() -> None:
    v = [1, 3, 2, 5, 4]
    assert highest(v, 3) == [None, None, 3, 5, 5]
    assert lowest(v, 3) == [None, None, 1, 2, 2]
    up, mid, lo = donchian(v, v, 2)
    assert up[-1] == 5 and lo[-1] == 4 and mid[-1] == 4.5


def test_adx_strong_in_trend_weak_in_range() -> None:
    trend = candles_from_closes([100 + i for i in range(80)])
    a, pdi, mdi = adx([c.high for c in trend], [c.low for c in trend], [c.close for c in trend], 14)
    assert a[-1] > 40 and pdi[-1] > mdi[-1]
    rng = candles_from_closes(sine_closes(200, amp=2, period=10))
    a2, _, _ = adx([c.high for c in rng], [c.low for c in rng], [c.close for c in rng], 14)
    assert a2[-1] < a[-1]
    assert all(x is None for x in a[:20])


def test_supertrend_flips_with_trend() -> None:
    closes = [100 + i for i in range(60)] + [160 - 2 * i for i in range(40)]
    c = candles_from_closes(closes)
    line, d = supertrend([x.high for x in c], [x.low for x in c], closes, 10, 3)
    assert d[55] == 1 and d[-1] == -1
    assert line[55] < closes[55] and line[-1] > closes[-1]


# ------------------------------------------------------------------ registry / params
def test_registered_and_described() -> None:
    assert NEW_KEYS <= set(STRATEGIES)
    described = {s["key"]: s for s in list_strategies()}
    for key in NEW_KEYS:
        names = {p["name"] for p in described[key]["params"]}
        assert {"atr_period", "atr_multiplier", "allow_short", "risk_fraction"} <= names


@pytest.mark.parametrize(
    "key,params",
    [
        ("rsi_mean_reversion", {"oversold": 40, "exit_level": 30}),
        ("donchian_breakout", {"entry_period": 10, "exit_period": 20}),
        ("ema_adx_trend", {"fast": 50, "slow": 20}),
        ("supertrend", {"atr_multiplier": 0}),
        ("bollinger_squeeze", {"bogus": 1}),
    ],
)
def test_param_validation(key, params) -> None:
    with pytest.raises(StrategyParamError):
        build_strategy(key, params)


# ------------------------------------------------------------------ signals
def test_rsi_mean_reversion_long_entry_and_exit() -> None:
    s = build_strategy("rsi_mean_reversion", {"rsi_period": 5})
    closes = [100.0] * 20 + [100 - 2 * i for i in range(1, 8)]  # oversold
    closes.append(closes[-1] + 3)  # RSI crosses back above 30
    e = assert_atr_entry(s.on_candle(ctx(closes)), OrderSide.BUY)
    assert e.stop_loss < Decimal(str(closes[-1]))
    exit_ = s.on_candle(ctx(closes + [closes[-1] + 6, closes[-1] + 10], qty="1"))
    assert exit_ and exit_[0].reduce_only and exit_[0].side == OrderSide.SELL


def test_rsi_mean_reversion_short_respects_allow_short() -> None:
    closes = [100.0] * 20 + [100 + 2 * i for i in range(1, 8)]
    closes.append(closes[-1] - 3)
    assert_atr_entry(build_strategy("rsi_mean_reversion", {"rsi_period": 5}).on_candle(ctx(closes)), OrderSide.SELL)
    no_short = build_strategy("rsi_mean_reversion", {"rsi_period": 5, "allow_short": False})
    assert no_short.on_candle(ctx(closes)) == []


def test_bollinger_reversion_reentry_from_lower_band() -> None:
    s = build_strategy("bollinger_reversion", {"bb_period": 10, "atr_period": 5})
    closes = [100 + (0.5 if i % 2 else -0.5) for i in range(30)] + [94.0, 99.5]
    e = assert_atr_entry(s.on_candle(ctx(closes)), OrderSide.BUY)
    assert e.tag == "bb_rev_long"
    out = s.on_candle(ctx(closes + [101.0], qty="1"))
    assert out[0].reduce_only


def test_bollinger_squeeze_breakout() -> None:
    s = build_strategy("bollinger_squeeze", {"bb_period": 10, "squeeze_lookback": 20, "atr_period": 5})
    wide = sine_closes(40, amp=8, period=8)
    tight = [100 + (0.05 if i % 2 else -0.05) for i in range(25)]
    e = assert_atr_entry(s.on_candle(ctx(wide + tight + [103.0])), OrderSide.BUY)
    assert e.tag == "bb_sq_long"
    assert s.on_candle(ctx(wide + tight + [100.0])) == []


def test_donchian_breakout_and_reverse() -> None:
    s = build_strategy("donchian_breakout", {"entry_period": 10, "exit_period": 5, "atr_period": 5})
    base = [100 + (1 if i % 2 else -1) for i in range(20)]
    e = assert_atr_entry(s.on_candle(ctx(base + [105.0])), OrderSide.BUY)
    assert e.stop_loss < Decimal("105")
    out = s.on_candle(ctx(base + [94.0], qty="1"))  # exit long below the 5-bar low, then short
    assert out[0].reduce_only and out[0].side == OrderSide.SELL


def test_ema_adx_trend_entry_and_trailing_exit() -> None:
    s = build_strategy("ema_adx_trend", {"fast": 5, "slow": 15, "adx_period": 7, "atr_period": 7, "trail_atr": 2})
    up = [100 + i for i in range(40)]
    assert_atr_entry(s.on_candle(ctx(up)), OrderSide.BUY)
    state: dict = {}
    assert s.on_candle(ctx(up, qty="1", entry="120", state=state)) == []
    level = state["trail"]["level"]
    assert level < up[-1]
    out = s.on_candle(ctx(up + [up[-1] - 10], qty="1", entry="120", state=state))
    assert out and out[0].reduce_only and out[0].tag == "ema_adx_trend_trail"
    assert "trail" not in state


def test_ema_adx_no_entry_in_range() -> None:
    s = build_strategy("ema_adx_trend", {"fast": 5, "slow": 15, "adx_period": 7, "adx_threshold": 40})
    assert s.on_candle(ctx(sine_closes(120, amp=1, period=6))) == []


def test_supertrend_flip_long_then_short() -> None:
    s = build_strategy("supertrend", {"st_period": 5, "st_multiplier": 2, "atr_period": 5})
    down = [150 - i for i in range(30)]
    seq = down + [down[-1] + 4 * i for i in range(1, 6)]
    signals = [entry_signal(s, "X", candles_from_closes(seq[:n])) for n in range(31, len(seq) + 1)]
    assert "long" in signals
    up = [100 + i for i in range(30)]
    seq2 = up + [up[-1] - 4 * i for i in range(1, 6)]
    signals2 = [entry_signal(s, "X", candles_from_closes(seq2[:n])) for n in range(31, len(seq2) + 1)]
    assert "short" in signals2


def test_entry_signal_helper_handles_short_history() -> None:
    assert entry_signal(build_strategy("supertrend"), "X", candles_from_closes([1, 2, 3])) is None


# ------------------------------------------------------------------ backtest smoke tests
def _market(n: int = 600) -> list[float]:
    out, p = [], 100.0
    for i in range(n):
        p *= 1 + 0.004 * math.sin(i / 15) + (0.002 if (i // 150) % 2 == 0 else -0.002)
        out.append(round(p, 4))
    return out


@pytest.mark.parametrize("cls", TECHNICAL_STRATEGIES, ids=lambda c: c.key)
def test_backtest_smoke(cls) -> None:
    res = run_backtest(cls({}), candles_from_closes(_market(), spread=0.004), "BTCUSDT", "1h")
    m = res.metrics
    assert m["num_fills"] >= 2, (cls.key, m, res.rejections)
    # risk-based sizing keeps every strategy within the account drawdown limit
    assert res.killed_at is None and m["max_drawdown_pct"] < 15
    assert "stop_loss_required" not in res.rejections
