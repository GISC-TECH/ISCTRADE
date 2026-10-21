"""Classic strategies (MACD, Turtle, RSI-2, Holy Grail, VWAP, Ichimoku, Heikin-Ashi, Keltner, Stoch RSI, 3 EMAs,
opening-range and volatility breakouts): signals on crafted series, mandatory stops, take profits, no lookahead,
backtest smoke tests and the preset catalogue."""

from __future__ import annotations

import math
import random
from decimal import Decimal

import pytest

from app.backtest.engine import BacktestConfig, run_backtest
from app.models.enums import OrderSide
from app.strategy.base import Candle, OrderIntent, Strategy, StrategyContext, StrategyParamError
from app.strategy.classic import CLASSIC_STRATEGIES, CLASSIC_STRATEGY_KEYS
from app.strategy.indicators import heikin_ashi, ichimoku, keltner, session_vwap, stoch_rsi
from app.strategy.strategies import STRATEGIES, build_strategy, entry_signal, list_strategies
from tests.factories import HOUR_MS, candles_from_closes

DAY_MS = 24 * HOUR_MS
D0 = 1_700_006_400_000  # 2023-11-15T00:00:00Z (UTC midnight)
assert D0 % DAY_MS == 0


# ------------------------------------------------------------------ helpers
def ctx(candles, qty="0", entry="0", state=None, params=None) -> StrategyContext:
    return StrategyContext(
        symbol="BTCUSDT",
        candles=candles,
        position_qty=Decimal(qty),
        position_entry=Decimal(entry),
        equity=Decimal("10000"),
        leverage=1,
        params=params or {},
        state=state if state is not None else {},
    )


def bars(closes, start=D0, step=HOUR_MS, spread=0.001, volume=100.0) -> list[Candle]:
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        hi, lo = max(prev, c) * (1 + spread), min(prev, c) * (1 - spread)
        out.append(Candle(start + i * step, prev, hi, lo, c, volume, start + (i + 1) * step - 1))
        prev = c
    return out


def first_entry(strategy: Strategy, candles: list[Candle], start: int | None = None):
    """Scan prefixes (flat position, fresh state) and return (index, entry intent) of the first entry."""
    for n in range(start or strategy.warmup, len(candles) + 1):
        for intent in strategy.on_candle(ctx(candles[:n])):
            if not intent.reduce_only:
                return n - 1, intent
    return None, None


def assert_protected(intent: OrderIntent, price: float) -> None:
    """Entries must carry a stop on the protective side; a TP (if any) on the profit side; size from risk."""
    assert not intent.reduce_only and intent.quantity is None
    assert intent.stop_loss is not None and intent.stop_loss > 0
    p = Decimal(str(price))
    if intent.side == OrderSide.BUY:
        assert intent.stop_loss < p
        assert intent.take_profit is None or intent.take_profit > p
    else:
        assert intent.stop_loss > p
        assert intent.take_profit is None or intent.take_profit < p


def market(n=900, seed=7, drift=0.002, vol=0.006) -> list[float]:
    rnd, out, p = random.Random(seed), [], 100.0
    for i in range(n):
        p *= 1 + 0.004 * math.sin(i / 15) + (drift if (i // 150) % 2 == 0 else -drift) + rnd.gauss(0, vol)
        out.append(round(p, 4))
    return out


# ------------------------------------------------------------------ registry / params
def test_classic_strategies_registered_and_described() -> None:
    assert len(CLASSIC_STRATEGIES) == 12
    assert set(CLASSIC_STRATEGY_KEYS) <= set(STRATEGIES)
    described = {s["key"]: s for s in list_strategies()}
    for key in CLASSIC_STRATEGY_KEYS:
        info = described[key]
        names = {p["name"] for p in info["params"]}
        assert {"atr_period", "atr_multiplier", "take_profit_r", "allow_short", "risk_fraction"} <= names
        assert info["category"] in ("tendência", "rompimento", "reversão") and info["description"]
        for p in info["params"]:  # every numeric parameter is bounded
            if p["type"] in ("number", "integer"):
                assert p["min"] is not None and p["max"] is not None, (key, p["name"])
                assert p["min"] <= p["default"] <= p["max"], (key, p["name"])


@pytest.mark.parametrize(
    "key,params",
    [
        ("macd_trend", {"fast": 30, "slow": 26}),
        ("turtle", {"exit_period": 25}),
        ("turtle", {"failsafe_period": 15}),
        ("triple_ema", {"fast": 30, "mid": 21}),
        ("ichimoku_breakout", {"tenkan": 30}),
        ("session_breakout", {"range_minutes": 480, "session_hours": 8}),
        ("connors_rsi2", {"rsi_period": 1}),
        ("stoch_rsi_reversal", {"oversold": 60}),
        ("vwap_reversion", {"band_std": 9}),
        ("keltner_breakout", {"take_profit_r": -1}),
        ("holy_grail", {"bogus": 1}),
    ],
)
def test_classic_param_validation(key, params) -> None:
    with pytest.raises(StrategyParamError):
        build_strategy(key, params)


# ------------------------------------------------------------------ indicators
def test_new_indicators_have_no_lookahead() -> None:
    cs = candles_from_closes(market(260), spread=0.003)
    o, h, lo, c = [x.open for x in cs], [x.high for x in cs], [x.low for x in cs], [x.close for x in cs]
    t, v = [x.open_time for x in cs], [x.volume + i % 7 for i, x in enumerate(cs)]
    full = {
        "stoch": stoch_rsi(c),
        "ha": heikin_ashi(o, h, lo, c),
        "kc": keltner(h, lo, c),
        "ich": tuple(ichimoku(h, lo).values()),
        "vwap": session_vwap(t, h, lo, c, v),
    }
    for n in (120, 181, 259):
        part = {
            "stoch": stoch_rsi(c[:n]),
            "ha": heikin_ashi(o[:n], h[:n], lo[:n], c[:n]),
            "kc": keltner(h[:n], lo[:n], c[:n]),
            "ich": tuple(ichimoku(h[:n], lo[:n]).values()),
            "vwap": session_vwap(t[:n], h[:n], lo[:n], c[:n], v[:n]),
        }
        for name in full:
            for series_full, series_part in zip(full[name], part[name]):
                assert list(series_full[:n]) == list(series_part), (name, n)


def test_indicator_values() -> None:
    k, d = stoch_rsi(market(200))
    assert all(0 <= x <= 100 for x in k if x is not None) and d[-1] is not None
    ho, hh, hl, hc = heikin_ashi([10, 11], [12, 13], [9, 10], [11, 12])
    assert hc == [10.5, 11.5] and ho == [10.5, 10.5] and hh[1] == 13 and hl[1] == 10
    ich = ichimoku([float(i) for i in range(1, 101)], [float(i) - 1 for i in range(1, 101)], 9, 26, 52, 26)
    raw_a = (ich["tenkan"][60] + ich["kijun"][60]) / 2
    assert ich["span_a"][86] == raw_a and ich["span_a"][25] is None
    # VWAP resets at the UTC day boundary
    times = [D0 - 2 * HOUR_MS, D0 - HOUR_MS, D0, D0 + HOUR_MS]
    vw, sd = session_vwap(times, [11, 11, 21, 21], [9, 9, 19, 19], [10, 10, 20, 20], [1, 1, 1, 1])
    assert vw == [10, 10, 20, 20] and sd[-1] == 0


# ------------------------------------------------------------------ crafted signals
def test_macd_trend_long_with_filter_and_blocked_against_trend() -> None:
    s = build_strategy("macd_trend", {"use_trend_filter": False, "allow_short": False})
    closes = [100.0] * 60 + [100 - i for i in range(1, 21)] + [80 + 1.5 * i for i in range(1, 30)]
    i, e = first_entry(s, bars(closes))
    assert e is not None and e.side == OrderSide.BUY and i > 80
    assert_protected(e, closes[i])
    # with the 200 EMA filter, the same rebound below a falling trend average is not bought
    down = [200 - 0.4 * i for i in range(240)] + [104 + 1.0 * i for i in range(1, 12)]
    f = build_strategy("macd_trend", {"use_trend_filter": True, "allow_short": False})
    assert first_entry(f, bars(down))[1] is None


def test_turtle_breakout_stop_2n_and_skip_after_win() -> None:
    s = build_strategy("turtle")
    closes = [100 + (1 if i % 2 else -1) for i in range(70)] + [104.0]
    cs = bars(closes, spread=0.002)
    intents = s.on_candle(ctx(cs))
    assert len(intents) == 1 and intents[0].side == OrderSide.BUY and intents[0].tag == "turtle_55_buy"
    e = intents[0]
    n = e.meta["n"]
    assert float(Decimal("104") - e.stop_loss) == pytest.approx(2 * n, rel=1e-6)
    # 20-candle breakout that is not a 55-candle breakout is skipped after a winning trade
    chop = [100 + (1 if i % 2 else -1) for i in range(30)]
    closes2 = chop + [110.0] * 5 + chop[:25] + [102.5]  # above the 20-candle high, below the 55-candle high
    cs2 = bars(closes2, spread=0.002)
    assert s.on_candle(ctx(cs2))[0].tag == "turtle_20_buy"
    st = {"last_win": True}
    assert s.on_candle(ctx(cs2, state=st)) == [] and st["last_win"] is None
    # exit on the 10-candle low records the result
    st = {}
    out = s.on_candle(ctx(bars(closes + [103, 102, 101, 100, 99, 98.0]), qty="1", entry="104", state=st))
    assert out and out[0].reduce_only and st["last_win"] is False
    s.on_stop_out(ctx(cs, state=st))
    assert st["last_win"] is False


def test_connors_rsi2_buys_dips_above_sma200_and_exits_above_sma5() -> None:
    s = build_strategy("connors_rsi2")
    closes = [100 + 0.3 * i for i in range(230)] + [168.0, 165.0, 162.0]
    cs = bars(closes)
    out = s.on_candle(ctx(cs))
    assert out and out[0].side == OrderSide.BUY and out[0].meta["rsi"] < 10
    assert_protected(out[0], closes[-1])
    # exit when the close is back above the 5 SMA
    up = closes + [170.0]
    ex = s.on_candle(ctx(bars(up), qty="1", entry="162"))
    assert ex and ex[0].reduce_only and ex[0].side == OrderSide.SELL
    # no long when the price is under the 200 SMA (downtrend dip -> no buy, and the short side needs RSI > 90)
    down = [200 - 0.3 * i for i in range(230)] + [128.0, 125.0, 122.0]
    assert all(i.side != OrderSide.BUY for i in s.on_candle(ctx(bars(down))))
    # time exit
    st = {"entry_time": cs[-12].open_time}
    t = build_strategy("connors_rsi2", {"max_bars": 10}).on_candle(ctx(cs, qty="1", entry="150", state=st))
    assert t and t[0].tag == "rsi2_time_exit"


def test_holy_grail_pullback_entry_structural_stop_and_swing_target() -> None:
    s = build_strategy("holy_grail")
    closes = [100 * 1.01**i for i in range(60)]
    cs = bars(closes, spread=0.002)
    from app.strategy.indicators import ema

    e20 = ema([c.close for c in cs], 20)[-1]
    last = cs[-1]
    # pullback candle whose low touches the EMA 20, then a close above its high
    pull = Candle(
        last.close_time + 1,
        last.close,
        last.close * 1.001,
        e20 * 0.999,
        last.close * 0.985,
        100,
        last.close_time + HOUR_MS,
    )
    trig_close = pull.high * 1.004
    trig = Candle(
        pull.close_time + 1,
        pull.close,
        trig_close * 1.001,
        pull.close * 0.999,
        trig_close,
        100,
        pull.close_time + HOUR_MS,
    )
    series = cs + [pull, trig]
    out = s.on_candle(ctx(series))
    assert out and out[0].side == OrderSide.BUY, out
    e = out[0]
    assert_protected(e, trig_close)
    assert e.stop_loss < Decimal(str(min(pull.low, trig.low)))
    swing = max(c.high for c in series[-21:-1])
    assert e.take_profit == Decimal(str(round(swing, 10))) or e.take_profit > Decimal(str(trig_close))
    # no entry without the pullback touching the EMA
    assert s.on_candle(ctx(bars(closes + [closes[-1] * 1.01], spread=0.002))) == []


def test_vwap_reversion_reentry_targets_vwap_and_flattens_at_day_end() -> None:
    s = build_strategy("vwap_reversion", {"min_session_bars": 6})
    step = 5 * 60_000
    closes = [100 + (0.2 if i % 2 else -0.2) for i in range(30)] + [97.0, 99.5]
    cs = bars(closes, start=D0, step=step)
    out = s.on_candle(ctx(cs))
    assert out and out[0].side == OrderSide.BUY
    e = out[0]
    assert_protected(e, 99.5)
    vw, _ = session_vwap(
        [c.open_time for c in cs],
        [c.high for c in cs],
        [c.low for c in cs],
        [c.close for c in cs],
        [c.volume for c in cs],
    )
    assert float(e.take_profit) == pytest.approx(vw[-1], rel=1e-9)
    # position closed on the last candle of the UTC day
    day_end = bars([100.0] * 288, start=D0, step=step)
    ex = s.on_candle(ctx(day_end, qty="1", entry="99"))
    assert ex and ex[0].tag == "vwap_session_end"
    # never trades on daily candles
    assert s.on_candle(ctx(bars(closes, step=DAY_MS))) == []


def test_ichimoku_cloud_breakout_long_and_kijun_exit() -> None:
    s = build_strategy("ichimoku_breakout")
    # decline, base, then a slow rally through the cloud (Tenkan above Kijun once the base low leaves the window)
    closes = [100 - 0.2 * i for i in range(90)] + [82.0] * 30 + [82 + 0.3 * i for i in range(1, 60)]
    i, e = first_entry(s, bars(closes))
    assert e is not None and e.side == OrderSide.BUY and i >= 120
    assert_protected(e, closes[i])
    crash = closes[: i + 1] + [closes[i] * 0.9]
    ex = s.on_candle(ctx(bars(crash), qty="1", entry=str(closes[i])))
    assert ex and ex[0].reduce_only


def test_heikin_ashi_flip_above_ema() -> None:
    s = build_strategy("heikin_ashi_ema")
    closes = [100 + 0.5 * i for i in range(70)] + [133.0, 132.0, 131.0, 133.0, 135.0]
    i, e = first_entry(s, bars(closes), start=71)
    assert e is not None and e.side == OrderSide.BUY
    assert_protected(e, closes[i])
    ex = s.on_candle(ctx(bars(closes[: i + 1] + [128.0]), qty="1", entry="133"))
    assert ex and ex[0].tag == "ha_exit_long"


def test_keltner_breakout_long_and_mid_exit() -> None:
    s = build_strategy("keltner_breakout", {"take_profit_r": 2, "trail_atr": 0})
    closes = [100 + (0.3 if i % 2 else -0.3) for i in range(40)] + [104.0]
    out = s.on_candle(ctx(bars(closes)))
    assert out and out[0].side == OrderSide.BUY
    e = out[0]
    assert_protected(e, 104.0)
    risk = Decimal("104") - e.stop_loss
    assert e.take_profit == pytest.approx(Decimal("104") + 2 * risk, rel=Decimal("1e-9"))
    ex = s.on_candle(ctx(bars(closes + [99.0]), qty="1", entry="104"))
    assert ex and ex[0].tag == "keltner_exit_long"


def test_stoch_rsi_cross_up_from_oversold() -> None:
    s = build_strategy("stoch_rsi_reversal", {"use_trend_filter": False})
    closes = [100 + 10 * math.sin(2 * math.pi * i / 40) for i in range(160)]
    i, e = first_entry(s, bars(closes))
    assert e is not None
    assert_protected(e, closes[i])
    if e.side == OrderSide.BUY:  # bought near a trough of the sine wave
        assert closes[i] < 100
    else:
        assert closes[i] > 100


def test_triple_ema_alignment_entry_and_exit() -> None:
    s = build_strategy("triple_ema")
    closes = [150 - 0.5 * i for i in range(80)] + [110 + 1.0 * i for i in range(1, 60)]
    i, e = first_entry(s, bars(closes), start=81)
    assert e is not None and e.side == OrderSide.BUY
    assert_protected(e, closes[i])


def test_session_breakout_range_stop_target_and_session_exit() -> None:
    s = build_strategy("session_breakout", {"session_start_hour": 13, "range_minutes": 60, "take_profit_r": 2})
    step = 15 * 60_000
    start = D0 + 13 * HOUR_MS - 20 * step  # 20 candles before the session opens
    pre = [100.0 + 0.1 * (i % 3) for i in range(20)]
    rng = [100.5, 99.5, 100.8, 99.8]  # opening range 13:00-14:00
    closes = pre + rng + [100.2, 102.0]
    cs = bars(closes, start=start, step=step, spread=0.0)
    hi, lo = max(c.high for c in cs[20:24]), min(c.low for c in cs[20:24])
    out = s.on_candle(ctx(cs))
    assert out and out[0].side == OrderSide.BUY and out[0].meta["range_high"] == hi
    e = out[0]
    assert e.stop_loss == Decimal(str(round(lo, 10)))
    assert e.take_profit == pytest.approx(Decimal("102") + 2 * (Decimal("102") - e.stop_loss), rel=Decimal("1e-9"))
    st: dict = {}
    s.on_result(e, True, ctx(cs, state=st))
    assert s.on_candle(ctx(bars(closes + [103.0], start=start, step=step, spread=0.0), state=st)) == []  # once
    # session end -> exit
    long_run = closes + [102.0] * 40
    ex = s.on_candle(ctx(bars(long_run[: 20 + 32], start=start, step=step), qty="1", entry="102", state=st))
    assert ex and ex[0].tag == "orb_session_end"
    # no entry before the opening range is complete, nor on candles larger than the range
    assert s.on_candle(ctx(bars(pre + [100.5, 103.0], start=start, step=step))) == []
    assert s.on_candle(ctx(bars(closes, start=start, step=4 * HOUR_MS))) == []


def test_volatility_breakout_entry_and_time_exit() -> None:
    s = build_strategy("volatility_breakout", {"use_trend_filter": False, "k": 0.5, "hold_bars": 2})
    cs = bars([100.0] * 30, spread=0.005)
    prev = cs[-1]
    big = Candle(prev.close_time + 1, 100.0, 102.0, 99.9, 101.8, 100, prev.close_time + HOUR_MS)
    out = s.on_candle(ctx(cs + [big]))
    assert out and out[0].side == OrderSide.BUY
    assert_protected(out[0], 101.8)
    st = {"entry_time": big.open_time}
    later = cs + [big] + bars([101.8, 101.9], start=big.close_time + 1)
    ex = s.on_candle(ctx(later, qty="1", entry="101.8", state=st))
    assert ex and ex[0].tag == "vbo_time_exit"
    assert s.on_candle(ctx(later[:-1], qty="1", entry="101.8", state=st)) == []


@pytest.mark.parametrize("cls", CLASSIC_STRATEGIES, ids=lambda c: c.key)
def test_allow_short_false_never_opens_shorts(cls) -> None:
    s = cls({"allow_short": False})
    for closes in (market(500, seed=3), market(500, seed=11, drift=-0.003)):
        cs = candles_from_closes(closes, spread=0.004)
        for n in range(s.warmup, len(cs) + 1, 3):
            assert all(i.side == OrderSide.BUY for i in s.on_candle(ctx(cs[:n])) if not i.reduce_only)


# ------------------------------------------------------------------ generic safety properties
def _intraday(closes, step=15 * 60_000):
    return bars(closes, start=D0, step=step, spread=0.003)


@pytest.mark.parametrize("cls", CLASSIC_STRATEGIES, ids=lambda c: c.key)
def test_every_entry_has_a_protective_stop(cls) -> None:
    s = cls({})
    entries = 0
    for seed in (1, 2, 5):
        closes = market(700, seed=seed)
        cs = _intraday(closes) if cls.key in ("session_breakout", "vwap_reversion") else bars(closes, spread=0.003)
        for n in range(s.warmup, len(cs) + 1):
            for intent in s.on_candle(ctx(cs[:n])):
                if not intent.reduce_only:
                    entries += 1
                    assert_protected(intent, cs[n - 1].close)
    assert entries > 0, cls.key


@pytest.mark.parametrize("cls", CLASSIC_STRATEGIES, ids=lambda c: c.key)
def test_backtest_has_no_lookahead(cls) -> None:
    """Trades up to candle N are identical whether or not the candles after N exist."""
    closes = market(700, seed=4)
    cs = _intraday(closes) if cls.key in ("session_breakout", "vwap_reversion") else bars(closes, spread=0.003)
    cfg = BacktestConfig(
        lookback=400, stop_on_kill_switch=False, max_drawdown_pct=Decimal("90"), max_daily_loss_pct=Decimal("50")
    )
    full = run_backtest(cls({}), cs, "BTCUSDT", "15m", cfg).trades
    cut = 450
    part = run_backtest(cls({}), cs[:cut], "BTCUSDT", "15m", cfg).trades
    limit = cs[cut - 1].close_time
    assert [t.as_dict() for t in full if t.time <= limit] == [t.as_dict() for t in part]


@pytest.mark.parametrize("cls", CLASSIC_STRATEGIES, ids=lambda c: c.key)
def test_classic_backtest_smoke(cls) -> None:
    closes = market(1200, seed=9)
    intraday = cls.key in ("session_breakout", "vwap_reversion")
    cs = _intraday(closes) if intraday else candles_from_closes(closes, spread=0.004)
    res = run_backtest(cls({}), cs, "BTCUSDT", "15m" if intraday else "1h")
    assert res.metrics["num_fills"] >= 2, (cls.key, res.metrics, res.rejections)
    assert "stop_loss_required" not in res.rejections
    assert res.metrics["max_drawdown_pct"] <= 16  # bounded by the account drawdown kill switch (15%)


@pytest.mark.parametrize("key", CLASSIC_STRATEGY_KEYS)
def test_scanner_entry_signal_runs(key) -> None:
    cs = candles_from_closes(market(300, seed=2), spread=0.004)
    assert entry_signal(build_strategy(key), "X", cs) in (None, "long", "short")
    assert entry_signal(build_strategy(key), "X", cs[:5]) is None


# ------------------------------------------------------------------ take profit in a paper bot
async def test_paper_bot_entry_carries_take_profit(db) -> None:
    from sqlalchemy import select

    from app.models import Position
    from app.services.bot_runner import run_bot_tick
    from app.services.execution import PaperExecutor
    from app.services.market_data import StaticMarketData
    from tests.factories import make_account, make_bot, make_user_workspace

    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    bot = await make_bot(db, acc, "keltner_breakout", {"take_profit_r": 2, "trail_atr": 0})
    await db.commit()
    closes = [100 + (0.3 if i % 2 else -0.3) for i in range(40)] + [104.0]
    market = StaticMarketData({"BTCUSDT": candles_from_closes(closes)})
    res = await run_bot_tick(db, bot, acc, market, executor=PaperExecutor(slippage_bps=Decimal(0)))
    assert res.filled == 1, res.reasons
    pos = await db.scalar(select(Position))
    assert pos.quantity > 0 and pos.stop_loss < Decimal("104") < pos.take_profit
