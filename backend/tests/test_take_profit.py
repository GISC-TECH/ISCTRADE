"""Optional take profit on strategy intents: simulated by the backtest engine (stop first when both are touched)."""

from decimal import Decimal

from app.backtest.engine import BacktestConfig, run_backtest
from app.models.enums import OrderSide
from app.strategy.base import Candle, OrderIntent, Strategy, StrategyContext
from tests.factories import HOUR_MS

D0 = 1_700_006_400_000


class _OneShot(Strategy):
    key = "_one_shot_test"
    name = "test"

    def __init__(self, stop: str, tp: str | None) -> None:
        super().__init__({})
        self.stop, self.tp = Decimal(stop), (Decimal(tp) if tp else None)

    def on_candle(self, ctx: StrategyContext) -> list[OrderIntent]:
        if len(ctx.candles) == 2 and ctx.position_qty == 0:
            return [OrderIntent(OrderSide.BUY, None, stop_loss=self.stop, take_profit=self.tp, tag="entry")]
        return []


def _tp_candles(path: list[tuple[float, float, float, float]]) -> list[Candle]:
    return [
        Candle(D0 + i * HOUR_MS, o, h, lo, c, 100, D0 + (i + 1) * HOUR_MS - 1) for i, (o, h, lo, c) in enumerate(path)
    ]


def test_backtest_take_profit_fills_at_target() -> None:
    path = [(100, 100, 100, 100), (100, 100, 100, 100), (100, 103, 99.5, 102), (102, 111, 101, 104)]
    cfg = BacktestConfig(slippage_bps=Decimal("0"))
    res = run_backtest(_OneShot("95", "110"), _tp_candles(path), "BTCUSDT", "1h", cfg)
    tags = [(t.tag, t.price) for t in res.trades]
    assert tags[0][0] == "entry" and tags[-1] == ("take_profit", Decimal("110"))
    assert res.trades[-1].realized_pnl > 0


def test_backtest_stop_wins_when_candle_touches_stop_and_target() -> None:
    path = [(100, 100, 100, 100), (100, 100, 100, 100), (100, 111, 94, 100)]
    res = run_backtest(_OneShot("95", "110"), _tp_candles(path), "BTCUSDT", "1h", BacktestConfig())
    assert [t.tag for t in res.trades] == ["entry", "stop_loss"]


def test_backtest_ignores_take_profit_on_the_wrong_side() -> None:
    path = [(100, 100, 100, 100), (100, 100, 100, 100), (100, 101, 90, 99)]
    res = run_backtest(_OneShot("85", "95"), _tp_candles(path), "BTCUSDT", "1h", BacktestConfig())
    assert [t.tag for t in res.trades] == ["entry"]
