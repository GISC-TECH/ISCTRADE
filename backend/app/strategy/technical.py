"""Technical strategies with ATR-based protective stops.

Every entry carries a stop loss ``price ∓ atr_multiplier × ATR`` (mandatory for the RiskManager, which also sizes
the position from the distance to that stop). Exits are reduce-only market intents. Trend strategies can also
trail the stop (``trail_atr`` ATRs from the best close since entry) and exit when a close crosses the trail.

All strategies are long/short unless ``allow_short`` is false, and work unchanged in bots and in the backtest.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, ClassVar

from app.models.enums import OrderSide
from app.strategy.base import OrderIntent, ParamSpec, Strategy, StrategyContext, StrategyParamError, to_decimal
from app.strategy.indicators import adx, atr, bollinger, donchian, ema, rsi, supertrend

COMMON_PARAMS = (
    ParamSpec("atr_period", "integer", 14, "Período do ATR", min=2, max=200),
    ParamSpec("atr_multiplier", "number", 2.0, "Distância do stop em ATRs", min=0.1, max=20),
    ParamSpec("allow_short", "boolean", True, "Permitir posições vendidas"),
    ParamSpec("risk_fraction", "number", 1.0, "Fração do risco máximo por trade usada", min=0.01, max=1),
)


class AtrStrategy(Strategy):
    """Shared helpers: ATR, entries with ATR stop, exits and optional trailing stop."""

    trailing: ClassVar[bool] = False

    def _atr(self, ctx: StrategyContext) -> float | None:
        c = ctx.candles
        v = atr([x.high for x in c], [x.low for x in c], ctx.closes, self.params["atr_period"])[-1]
        return v if v and v > 0 else None

    def _entry(self, ctx: StrategyContext, side: OrderSide, atr_v: float, tag: str) -> list[OrderIntent]:
        if side == OrderSide.SELL and not self.params["allow_short"]:
            return []
        price = ctx.price
        distance = to_decimal(atr_v) * to_decimal(self.params["atr_multiplier"])
        stop = price - distance if side == OrderSide.BUY else price + distance
        if stop <= 0:
            return []
        return [
            OrderIntent(
                side,
                None,
                stop_loss=stop,
                tag=tag,
                risk_fraction=to_decimal(self.params["risk_fraction"]),
                meta={"atr": round(atr_v, 8)},
            )
        ]

    @staticmethod
    def _exit(ctx: StrategyContext, tag: str) -> list[OrderIntent]:
        pos = ctx.position_qty
        if pos > 0:
            return [OrderIntent(OrderSide.SELL, pos, reduce_only=True, tag=tag)]
        if pos < 0:
            return [OrderIntent(OrderSide.BUY, -pos, reduce_only=True, tag=tag)]
        return []

    def _trail_hit(self, ctx: StrategyContext, atr_v: float) -> bool:
        """Update the trailing stop in ctx.state and return True when the last close crossed it."""
        mult = self.params.get("trail_atr") or 0
        pos = ctx.position_qty
        if not mult or pos == 0:
            ctx.state.pop("trail", None)
            return False
        close = ctx.closes[-1]
        side = 1 if pos > 0 else -1
        candidate = close - side * mult * atr_v
        trail = ctx.state.get("trail")
        if trail is None or trail.get("side") != side:
            entry = float(ctx.position_entry) if ctx.position_entry else close
            trail = {"side": side, "level": entry - side * mult * atr_v}
        hit = (side == 1 and close < trail["level"]) or (side == -1 and close > trail["level"])
        if not hit:
            trail["level"] = max(trail["level"], candidate) if side == 1 else min(trail["level"], candidate)
        ctx.state["trail"] = trail
        return hit

    def on_stop_out(self, ctx: StrategyContext) -> None:
        ctx.state.pop("trail", None)

    def on_candle(self, ctx: StrategyContext) -> list[OrderIntent]:
        if len(ctx.candles) < self.warmup:
            return []
        atr_v = self._atr(ctx)
        if atr_v is None:
            return []
        if self.trailing and self._trail_hit(ctx, atr_v):
            ctx.state.pop("trail", None)
            return self._exit(ctx, f"{self.key}_trail")
        return self.signal(ctx, atr_v)

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:  # pragma: no cover - abstract
        raise NotImplementedError


def _reverse(strategy: AtrStrategy, ctx: StrategyContext, side: OrderSide, atr_v: float, tag: str) -> list[OrderIntent]:
    """Close an opposite position (if any) and open `side` when flat afterwards."""
    pos = ctx.position_qty
    intents: list[OrderIntent] = []
    if (side == OrderSide.BUY and pos < 0) or (side == OrderSide.SELL and pos > 0):
        intents += strategy._exit(ctx, f"{tag}_reverse")
        pos = Decimal(0)
    if pos == 0:
        intents += strategy._entry(ctx, side, atr_v, tag)
    return intents


class RsiMeanReversionStrategy(AtrStrategy):
    key = "rsi_mean_reversion"
    name = "RSI reversão à média (ATR stop)"
    description = (
        "Compra quando o RSI sai da zona de sobrevenda (vende quando sai da sobrecompra); "
        "encerra quando o RSI volta à linha central. Melhor em mercados laterais."
    )
    params_spec = (
        ParamSpec("rsi_period", "integer", 14, "Período do RSI", min=2, max=100),
        ParamSpec("oversold", "number", 30.0, "Nível de sobrevenda", min=1, max=49),
        ParamSpec("overbought", "number", 70.0, "Nível de sobrecompra", min=51, max=99),
        ParamSpec("exit_level", "number", 50.0, "RSI de saída (linha central)", min=20, max=80),
        *COMMON_PARAMS,
    )

    @classmethod
    def check_params(cls, params: dict[str, Any]) -> None:
        if not params["oversold"] < params["exit_level"] < params["overbought"]:
            raise StrategyParamError("oversold < exit_level < overbought")

    @property
    def warmup(self) -> int:
        return max(self.params["rsi_period"], self.params["atr_period"]) + 3

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        r = rsi(ctx.closes, self.params["rsi_period"])
        prev, cur = r[-2], r[-1]
        if prev is None or cur is None:
            return []
        pos, p = ctx.position_qty, self.params
        if pos > 0 and cur >= p["exit_level"]:
            return self._exit(ctx, "rsi_mr_exit_long")
        if pos < 0 and cur <= p["exit_level"]:
            return self._exit(ctx, "rsi_mr_exit_short")
        if pos == 0 and prev < p["oversold"] <= cur:
            return self._entry(ctx, OrderSide.BUY, atr_v, "rsi_mr_long")
        if pos == 0 and prev > p["overbought"] >= cur:
            return self._entry(ctx, OrderSide.SELL, atr_v, "rsi_mr_short")
        return []


class BollingerReversionStrategy(AtrStrategy):
    key = "bollinger_reversion"
    name = "Bollinger reversão à média"
    description = (
        "Compra quando o preço fecha de volta para dentro da banda inferior (vende na superior); "
        "alvo na média central. Evite em tendências fortes."
    )
    params_spec = (
        ParamSpec("bb_period", "integer", 20, "Período das bandas", min=5, max=200),
        ParamSpec("bb_std", "number", 2.0, "Desvios-padrão", min=0.5, max=5),
        *COMMON_PARAMS,
    )

    @property
    def warmup(self) -> int:
        return max(self.params["bb_period"], self.params["atr_period"]) + 3

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        closes = ctx.closes
        mid, up, lo = bollinger(closes, self.params["bb_period"], self.params["bb_std"])
        if None in (mid[-1], up[-2], lo[-2], up[-1], lo[-1]):
            return []
        pos = ctx.position_qty
        if pos > 0 and closes[-1] >= mid[-1]:  # type: ignore[operator]
            return self._exit(ctx, "bb_rev_exit_long")
        if pos < 0 and closes[-1] <= mid[-1]:  # type: ignore[operator]
            return self._exit(ctx, "bb_rev_exit_short")
        if pos == 0 and closes[-2] < lo[-2] and closes[-1] >= lo[-1]:  # type: ignore[operator]
            return self._entry(ctx, OrderSide.BUY, atr_v, "bb_rev_long")
        if pos == 0 and closes[-2] > up[-2] and closes[-1] <= up[-1]:  # type: ignore[operator]
            return self._entry(ctx, OrderSide.SELL, atr_v, "bb_rev_short")
        return []


class BollingerSqueezeStrategy(AtrStrategy):
    key = "bollinger_squeeze"
    name = "Bollinger squeeze breakout"
    description = (
        "Após uma compressão (largura das bandas na mínima de N candles), entra no rompimento da banda; "
        "sai na média central ou no trailing stop por ATR."
    )
    trailing = True
    params_spec = (
        ParamSpec("bb_period", "integer", 20, "Período das bandas", min=5, max=200),
        ParamSpec("bb_std", "number", 2.0, "Desvios-padrão", min=0.5, max=5),
        ParamSpec("squeeze_lookback", "integer", 50, "Candles para medir a compressão", min=10, max=500),
        ParamSpec("squeeze_tolerance", "number", 1.1, "Largura ≤ mínima × tolerância", min=1, max=3),
        ParamSpec("trail_atr", "number", 3.0, "Trailing stop em ATRs (0 = desliga)", min=0, max=20),
        *COMMON_PARAMS,
    )

    @property
    def warmup(self) -> int:
        return self.params["bb_period"] + self.params["squeeze_lookback"] + 2

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        closes = ctx.closes
        mid, up, lo = bollinger(closes, self.params["bb_period"], self.params["bb_std"])
        if None in (mid[-1], up[-1], lo[-1], mid[-2]):
            return []
        pos = ctx.position_qty
        if pos > 0 and closes[-1] < mid[-1]:  # type: ignore[operator]
            return self._exit(ctx, "bb_sq_exit_long")
        if pos < 0 and closes[-1] > mid[-1]:  # type: ignore[operator]
            return self._exit(ctx, "bb_sq_exit_short")
        if pos != 0:
            return []
        widths = [
            (u - lw) / m for u, lw, m in zip(up[:-1], lo[:-1], mid[:-1]) if u is not None and lw is not None and m
        ][-self.params["squeeze_lookback"] :]
        if len(widths) < self.params["squeeze_lookback"]:
            return []
        squeezed = widths[-1] <= min(widths) * self.params["squeeze_tolerance"]
        if not squeezed:
            return []
        if closes[-1] > up[-1]:  # type: ignore[operator]
            return self._entry(ctx, OrderSide.BUY, atr_v, "bb_sq_long")
        if closes[-1] < lo[-1]:  # type: ignore[operator]
            return self._entry(ctx, OrderSide.SELL, atr_v, "bb_sq_short")
        return []


class DonchianBreakoutStrategy(AtrStrategy):
    key = "donchian_breakout"
    name = "Rompimento de Donchian (range breakout)"
    description = (
        "Entra quando o fechamento rompe a máxima/mínima dos últimos N candles (estilo Turtle); "
        "sai no canal oposto mais curto ou no trailing stop por ATR."
    )
    trailing = True
    params_spec = (
        ParamSpec("entry_period", "integer", 20, "Canal de entrada (candles)", min=5, max=500),
        ParamSpec("exit_period", "integer", 10, "Canal de saída (candles)", min=2, max=500),
        ParamSpec("trail_atr", "number", 0.0, "Trailing stop em ATRs (0 = desliga)", min=0, max=20),
        *COMMON_PARAMS,
    )

    @classmethod
    def check_params(cls, params: dict[str, Any]) -> None:
        if params["exit_period"] >= params["entry_period"]:
            raise StrategyParamError("exit_period must be smaller than entry_period")

    @property
    def warmup(self) -> int:
        return max(self.params["entry_period"], self.params["atr_period"]) + 2

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        c = ctx.candles
        highs, lows, closes = [x.high for x in c], [x.low for x in c], ctx.closes
        # channels of the PREVIOUS candles (exclude the current one)
        up, _, _ = donchian(highs[:-1], lows[:-1], self.params["entry_period"])
        xu, _, xl = donchian(highs[:-1], lows[:-1], self.params["exit_period"])
        _, _, lo = donchian(highs[:-1], lows[:-1], self.params["entry_period"])
        if None in (up[-1], lo[-1], xu[-1], xl[-1]):
            return []
        pos, close = ctx.position_qty, closes[-1]
        if pos > 0 and close < xl[-1]:  # type: ignore[operator]
            return self._exit(ctx, "donchian_exit_long")
        if pos < 0 and close > xu[-1]:  # type: ignore[operator]
            return self._exit(ctx, "donchian_exit_short")
        if close > up[-1]:  # type: ignore[operator]
            return _reverse(self, ctx, OrderSide.BUY, atr_v, "donchian_long")
        if close < lo[-1]:  # type: ignore[operator]
            return _reverse(self, ctx, OrderSide.SELL, atr_v, "donchian_short")
        return []


class EmaAdxTrendStrategy(AtrStrategy):
    key = "ema_adx_trend"
    name = "Tendência EMA + filtro ADX (trailing ATR)"
    description = (
        "Segue a tendência quando EMA rápida > lenta (ou <) e o ADX indica tendência forte; "
        "saída por trailing stop em ATRs ou inversão das médias."
    )
    trailing = True
    params_spec = (
        ParamSpec("fast", "integer", 20, "EMA rápida", min=2, max=500),
        ParamSpec("slow", "integer", 50, "EMA lenta", min=3, max=1000),
        ParamSpec("adx_period", "integer", 14, "Período do ADX", min=2, max=100),
        ParamSpec("adx_threshold", "number", 25.0, "ADX mínimo para entrar", min=5, max=80),
        ParamSpec("trail_atr", "number", 3.0, "Trailing stop em ATRs (0 = desliga)", min=0, max=20),
        *COMMON_PARAMS,
    )

    @classmethod
    def check_params(cls, params: dict[str, Any]) -> None:
        if params["fast"] >= params["slow"]:
            raise StrategyParamError("fast must be smaller than slow")

    @property
    def warmup(self) -> int:
        return max(self.params["slow"], 2 * self.params["adx_period"], self.params["atr_period"]) + 3

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        c = ctx.candles
        closes = ctx.closes
        fast, slow = ema(closes, self.params["fast"])[-1], ema(closes, self.params["slow"])[-1]
        a, pdi, mdi = adx([x.high for x in c], [x.low for x in c], closes, self.params["adx_period"])
        if None in (fast, slow, a[-1], pdi[-1], mdi[-1]):
            return []
        pos = ctx.position_qty
        up = fast > slow and pdi[-1] > mdi[-1]  # type: ignore[operator]
        down = fast < slow and mdi[-1] > pdi[-1]  # type: ignore[operator]
        if pos > 0 and fast < slow:  # type: ignore[operator]
            return self._exit(ctx, "ema_adx_exit_long")
        if pos < 0 and fast > slow:  # type: ignore[operator]
            return self._exit(ctx, "ema_adx_exit_short")
        strong = a[-1] >= self.params["adx_threshold"]  # type: ignore[operator]
        if pos == 0 and strong and up:
            return self._entry(ctx, OrderSide.BUY, atr_v, "ema_adx_long")
        if pos == 0 and strong and down:
            return self._entry(ctx, OrderSide.SELL, atr_v, "ema_adx_short")
        return []


class SupertrendStrategy(AtrStrategy):
    key = "supertrend"
    name = "Supertrend (ATR)"
    description = (
        "Entra na virada do Supertrend (linha por ATR) e inverte/encerra na virada oposta; "
        "o stop inicial é o próprio ATR × multiplicador."
    )
    params_spec = (
        ParamSpec("st_period", "integer", 10, "Período do ATR do Supertrend", min=2, max=200),
        ParamSpec("st_multiplier", "number", 3.0, "Multiplicador do Supertrend", min=0.5, max=10),
        *COMMON_PARAMS,
    )

    @property
    def warmup(self) -> int:
        return max(self.params["st_period"], self.params["atr_period"]) + 3

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        c = ctx.candles
        _, direction = supertrend(
            [x.high for x in c], [x.low for x in c], ctx.closes, self.params["st_period"], self.params["st_multiplier"]
        )
        prev, cur = direction[-2], direction[-1]
        if prev == 0 or cur == prev:
            return []
        if cur == 1:
            return _reverse(self, ctx, OrderSide.BUY, atr_v, "supertrend_long")
        if ctx.position_qty > 0 and not self.params["allow_short"]:
            return self._exit(ctx, "supertrend_exit_long")
        return _reverse(self, ctx, OrderSide.SELL, atr_v, "supertrend_short")


TECHNICAL_STRATEGIES: tuple[type[Strategy], ...] = (
    RsiMeanReversionStrategy,
    BollingerReversionStrategy,
    BollingerSqueezeStrategy,
    DonchianBreakoutStrategy,
    EmaAdxTrendStrategy,
    SupertrendStrategy,
)
