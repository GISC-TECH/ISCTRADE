"""Mean reversion: buys oversold RSI below the lower Bollinger band, exits at the middle band."""

from __future__ import annotations

from app.models.enums import OrderSide
from app.strategy.base import OrderIntent, ParamSpec, Strategy, StrategyContext, to_decimal
from app.strategy.indicators import bollinger, rsi


class RsiBollingerStrategy(Strategy):
    key = "rsi_bollinger"
    name = "RSI + Bollinger (reversão, long)"
    description = "Compra com RSI sobrevendido abaixo da banda inferior; sai na média móvel central."
    params_spec = (
        ParamSpec("rsi_period", "integer", 14, min=2, max=100),
        ParamSpec("oversold", "number", 30.0, min=1, max=50),
        ParamSpec("bb_period", "integer", 20, min=5, max=200),
        ParamSpec("bb_std", "number", 2.0, min=0.5, max=5),
        ParamSpec("stop_loss_pct", "number", 2.0, min=0.1, max=50),
        ParamSpec("risk_fraction", "number", 1.0, min=0.01, max=1),
    )

    @property
    def warmup(self) -> int:
        return max(self.params["rsi_period"], self.params["bb_period"]) + 2

    def on_candle(self, ctx: StrategyContext) -> list[OrderIntent]:
        if len(ctx.candles) < self.warmup:
            return []
        closes = ctx.closes
        r = rsi(closes, self.params["rsi_period"])[-1]
        mid, _, lower = bollinger(closes, self.params["bb_period"], self.params["bb_std"])
        if r is None or lower[-1] is None or mid[-1] is None:
            return []
        price = ctx.price
        if ctx.position_qty > 0 and closes[-1] >= mid[-1]:
            return [OrderIntent(OrderSide.SELL, ctx.position_qty, reduce_only=True, tag="rsi_exit")]
        if ctx.position_qty == 0 and r < self.params["oversold"] and closes[-1] < lower[-1]:
            stop = price * (1 - to_decimal(self.params["stop_loss_pct"]) / 100)
            return [
                OrderIntent(
                    OrderSide.BUY,
                    None,
                    stop_loss=stop,
                    tag="rsi_long",
                    risk_fraction=to_decimal(self.params["risk_fraction"]),
                )
            ]
        return []
