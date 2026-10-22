"""Funding-rate contrarian strategy (F-14): signals, backtest without look-ahead and the live bot funding feed."""

from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.backtest.engine import BacktestConfig, run_backtest
from app.models.trading import Position
from app.services.bot_runner import run_bot_tick
from app.services.execution import PaperExecutor
from app.services.market_data import BinanceMarketData, StaticMarketData
from app.strategy.base import FundingEvent, StrategyContext, StrategyParamError
from app.strategy.strategies import STRATEGIES, build_strategy
from tests.factories import HOUR_MS, candles_from_closes, make_account, make_bot, make_user_workspace

EIGHT_H = 8 * HOUR_MS
T0 = 1_704_067_200_000  # 2024-01-01T00:00:00Z
PARAMS = {"z_entry": 1.5, "confirm_ema": 10, "atr_period": 5, "lookback_events": 20}


def normal_funding(n: int, start: int = T0, base: str = "0.0001") -> list[FundingEvent]:
    return [FundingEvent(start + i * EIGHT_H, D(base) + D(i % 3) * D("0.00002")) for i in range(n)]


def ctx_for(strategy, closes, funding, qty=D(0), state=None) -> StrategyContext:
    candles = candles_from_closes(closes, start=T0)
    return StrategyContext(
        symbol="BTCUSDT", candles=candles, position_qty=qty, position_entry=D(str(closes[-1])) if qty else D(0),
        equity=D(10_000), leverage=1, params=strategy.params, state=state if state is not None else {},
        funding=funding,
    )  # fmt: skip


FALLING = [100 + i * 0.1 for i in range(20)] + [102 - i * 0.5 for i in range(10)]  # close < EMA at the end
RISING = [100 - i * 0.1 for i in range(20)] + [98 + i * 0.5 for i in range(10)]


def test_registered_and_param_validation() -> None:
    assert "funding_contrarian" in STRATEGIES and STRATEGIES["funding_contrarian"].needs_funding
    s = build_strategy("funding_contrarian", {})
    assert s.params["entry_rate"] == 0.0005 and s.funding_lookback == 31
    with pytest.raises(StrategyParamError):
        build_strategy("funding_contrarian", {"entry_rate": 0.0005, "exit_rate": 0.0006})


def test_extreme_positive_funding_shorts_with_confirmation() -> None:
    s = build_strategy("funding_contrarian", PARAMS)
    funding = [*normal_funding(25), FundingEvent(T0 + 25 * EIGHT_H, D("0.002"))]
    state: dict = {}
    [intent] = s.on_candle(ctx_for(s, FALLING, funding, state=state))
    assert intent.side.value == "SELL" and not intent.reduce_only
    assert intent.stop_loss > D(str(FALLING[-1])) > intent.take_profit
    assert intent.meta["funding"] == 0.002 and intent.meta["z"] >= 1.5
    # one entry per settled funding event: after a stop-out on the same reading, no immediate re-entry
    assert s.on_candle(ctx_for(s, FALLING, funding, state=state)) == []
    # price still rising -> no confirmation -> no trade; without confirmation it trades
    assert s.on_candle(ctx_for(s, RISING, funding)) == []
    s2 = build_strategy("funding_contrarian", {**PARAMS, "confirm_ema": 0})
    assert s2.on_candle(ctx_for(s2, RISING, funding))[0].side.value == "SELL"
    # allow_short=False blocks shorts
    s3 = build_strategy("funding_contrarian", {**PARAMS, "allow_short": False})
    assert s3.on_candle(ctx_for(s3, FALLING, funding)) == []


def test_extreme_negative_funding_goes_long_and_gates() -> None:
    s = build_strategy("funding_contrarian", PARAMS)
    funding = [*normal_funding(25), FundingEvent(T0 + 25 * EIGHT_H, D("-0.0015"))]
    [intent] = s.on_candle(ctx_for(s, RISING, funding))
    assert intent.side.value == "BUY" and intent.stop_loss < D(str(RISING[-1])) < intent.take_profit
    assert s.on_candle(ctx_for(s, RISING, [])) == []  # no funding data -> never trades
    assert s.on_candle(ctx_for(s, RISING, normal_funding(26))) == []  # normal funding -> nothing
    # extreme in absolute terms but not unusual vs. its own history -> z gate blocks
    high = [FundingEvent(T0 + i * EIGHT_H, D("-0.0015") - D(i % 2) * D("0.0001")) for i in range(26)]
    assert s.on_candle(ctx_for(s, RISING, high)) == []
    s0 = build_strategy("funding_contrarian", {**PARAMS, "z_entry": 0})
    assert s0.on_candle(ctx_for(s0, RISING, high))[0].side.value == "BUY"


def test_exits_when_funding_normalizes_or_time_limit() -> None:
    s = build_strategy("funding_contrarian", {**PARAMS, "max_hold_bars": 3})
    extreme = [*normal_funding(25), FundingEvent(T0 + 25 * EIGHT_H, D("0.002"))]
    state = {"held": 0, "entered_on": extreme[-1].time}
    assert s.on_candle(ctx_for(s, FALLING, extreme, qty=D("-1"), state=state)) == []
    [exit_] = s.on_candle(ctx_for(s, FALLING, [*extreme, FundingEvent(T0 + 26 * EIGHT_H, D("0.00005"))],
                                  qty=D("-1"), state=state))  # fmt: skip
    assert exit_.reduce_only and exit_.side.value == "BUY" and exit_.tag == "funding_normalized"
    state = {"held": 1}
    assert s.on_candle(ctx_for(s, FALLING, extreme, qty=D("-1"), state=state)) == []
    [exit_] = s.on_candle(ctx_for(s, FALLING, extreme, qty=D("-1"), state=state))
    assert exit_.tag == "funding_time_exit"


def scenario():
    """10 days of normal funding, then a spike settled at hour 240 while the price starts to fall."""
    closes = [100 + (0.2 if i % 2 else -0.2) for i in range(240)] + [100 - i * 0.15 for i in range(1, 49)]
    candles = candles_from_closes(closes, start=T0)
    funding = normal_funding(30)
    funding += [FundingEvent(T0 + 30 * EIGHT_H, D("0.0025")), FundingEvent(T0 + 31 * EIGHT_H, D("0.0025"))]
    funding += [FundingEvent(T0 + i * EIGHT_H, D("0.00005")) for i in range(32, 36)]
    return candles, funding


def test_backtest_uses_settled_funding_only_and_short_receives_funding() -> None:
    candles, funding = scenario()
    cfg = BacktestConfig(fee_rate=D(0), slippage_bps=D(0))
    s = build_strategy("funding_contrarian", {**PARAMS, "take_profit_rr": 0})
    res = run_backtest(s, candles, "BTCUSDT", "1h", cfg, funding=funding)
    entries = [t for t in res.trades if t.tag == "funding_contrarian"]
    assert entries and entries[0].side == "SELL"
    assert entries[0].time >= T0 + 30 * EIGHT_H  # never before the spike is settled (no look-ahead)
    assert res.metrics["total_funding"] > 0  # short receives positive funding
    assert any(t.tag in {"funding_normalized", "funding_time_exit"} for t in res.trades)
    assert res.metrics["return_pct"] > 0
    # no funding -> no trades
    res0 = run_backtest(build_strategy("funding_contrarian", PARAMS), candles, "BTCUSDT", "1h", cfg)
    assert not res0.trades


async def test_bot_tick_feeds_funding_history(db) -> None:
    candles, funding = scenario()
    candles = candles[:245]  # spike settled at 240, price already below its EMA
    _, ws = await make_user_workspace(db, email="funding@example.com")
    acc = await make_account(db, ws)
    bot = await make_bot(db, acc, "funding_contrarian", {**PARAMS, "risk_fraction": 0.9}, symbol="AAAUSDT")
    await db.commit()
    # funding after the last closed candle must be ignored (no look-ahead from the feed)
    market = StaticMarketData({"AAAUSDT": candles}, {"AAAUSDT": funding[:31]})
    res = await run_bot_tick(db, bot, acc, market, executor=PaperExecutor())
    pos = await db.scalar(select(Position).where(Position.account_id == acc.id, Position.symbol == "AAAUSDT"))
    assert res.filled == 1 and pos is not None and pos.quantity < 0

    _, ws2 = await make_user_workspace(db, email="nofunding@example.com")
    acc2 = await make_account(db, ws2)
    bot2 = await make_bot(db, acc2, "funding_contrarian", PARAMS, symbol="AAAUSDT")
    await db.commit()
    res2 = await run_bot_tick(db, bot2, acc2, StaticMarketData({"AAAUSDT": candles}), executor=PaperExecutor())
    assert res2.filled == 0


class FakeClient:
    def __init__(self, fail: bool = False) -> None:
        self.fail, self.calls = fail, []

    async def get_funding_rate_history(self, symbol, start_time=None, end_time=None, limit=100):
        self.calls.append((symbol, limit))
        if self.fail:
            raise RuntimeError("boom")
        return [{"symbol": symbol, "fundingTime": T0 + i * EIGHT_H, "fundingRate": "0.0001"} for i in range(5)][::-1]


async def test_binance_market_data_funding_history() -> None:
    client = FakeClient()
    md = BinanceMarketData(client=client)
    events = await md.get_funding_history("BTCUSDT", 3)
    assert [e.time for e in events] == [T0 + i * EIGHT_H for i in (2, 3, 4)] and events[0].rate == D("0.0001")
    assert client.calls == [("BTCUSDT", 3)]
