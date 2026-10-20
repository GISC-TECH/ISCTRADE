"""Trend following: EMA crossover with ATR-based stop loss and risk-based position sizing."""

from __future__ import annotations

from typing import Any

from app.models.enums import OrderSide
from app.strategy.base import OrderIntent, ParamSpec, Strategy, StrategyContext, StrategyParamError, to_decimal
from app.strategy.indicators import atr, crossed_above, crossed_below, ema


class EmaCrossStrategy(Strategy):
    key = "ema_cross"
    name = "EMA Cross + ATR stop"
    description = "Compra quando a EMA rápida cruza acima da lenta (vende/short no cruzamento inverso)."
    params_spec = (
        ParamSpec("fast", "integer", 12, "Período da EMA rápida", min=2, max=500),
        ParamSpec("slow", "integer", 26, "Período da EMA lenta", min=3, max=1000),
        ParamSpec("atr_period", "integer", 14, "Período do ATR", min=2, max=200),
        ParamSpec("atr_multiplier", "number", 2.0, "Distância do stop em ATRs", min=0.1, max=20),
        ParamSpec("allow_short", "boolean", True, "Permitir posições vendidas"),
        ParamSpec("risk_fraction", "number", 1.0, "Fração do risco máximo por trade usada", min=0.01, max=1),
    )

    @classmethod
    def check_params(cls, params: dict[str, Any]) -> None:
        if params["fast"] >= params["slow"]:
            raise StrategyParamError("fast must be smaller than slow")

    @property
    def warmup(self) -> int:
        return max(self.params["slow"], self.params["atr_period"]) + 2

    def on_candle(self, ctx: StrategyContext) -> list[OrderIntent]:
        if len(ctx.candles) < self.warmup:
            return []
        closes = ctx.closes
        fast = ema(closes, self.params["fast"])
        slow = ema(closes, self.params["slow"])
        atr_v = atr([c.high for c in ctx.candles], [c.low for c in ctx.candles], closes, self.params["atr_period"])[-1]
        if atr_v is None or atr_v <= 0:
            return []
        price = ctx.price
        distance = to_decimal(atr_v) * to_decimal(self.params["atr_multiplier"])
        risk_fraction = to_decimal(self.params["risk_fraction"])
        pos = ctx.position_qty
        intents: list[OrderIntent] = []

        if crossed_above(fast, slow):
            if pos < 0:
                intents.append(OrderIntent(OrderSide.BUY, -pos, reduce_only=True, tag="ema_exit_short"))
            if pos <= 0:
                intents.append(
                    OrderIntent(
                        OrderSide.BUY,
                        None,
                        stop_loss=price - distance,
                        tag="ema_long",
                        risk_fraction=risk_fraction,
                    )
                )
        elif crossed_below(fast, slow):
            if pos > 0:
                intents.append(OrderIntent(OrderSide.SELL, pos, reduce_only=True, tag="ema_exit_long"))
            if pos >= 0 and self.params["allow_short"]:
                intents.append(
                    OrderIntent(
                        OrderSide.SELL,
                        None,
                        stop_loss=price + distance,
                        tag="ema_short",
                        risk_fraction=risk_fraction,
                    )
                )
        return intents
