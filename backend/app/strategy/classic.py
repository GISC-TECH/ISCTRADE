"""Classic, well-known trading systems re-implemented from their published rules.

These are independent re-implementations of public ideas (books, articles, classic indicator setups); no code was
copied from third-party strategy repositories. Sources/inspirations are listed in ``docs/STRATEGIES.md``.

Every entry carries a protective stop (ATR-based or structural with an ATR floor), so the RiskManager can size the
position from the stop distance, and may carry a take profit (``take_profit_r`` × risk, or a structural target).
All signals use closed candles only: index ``-1`` is the last CLOSED candle and channels/ranges that act as entry
levels are computed from the candles *before* it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, ClassVar

from app.models.enums import OrderSide
from app.strategy.base import OrderIntent, ParamSpec, StrategyContext, StrategyParamError, to_decimal
from app.strategy.indicators import (
    adx,
    crossed_above,
    crossed_below,
    donchian,
    ema,
    heikin_ashi,
    ichimoku,
    keltner,
    macd,
    rsi,
    session_vwap,
    sma,
    stoch_rsi,
)
from app.strategy.technical import AtrStrategy

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
MIN_STOP_ATR = 0.25  # structural stops are never closer than this many ATRs (avoids oversized positions)


def base_params(atr_multiplier: float = 2.0, take_profit_r: float = 0.0, atr_period: int = 14) -> tuple[ParamSpec, ...]:
    return (
        ParamSpec("atr_period", "integer", atr_period, "Período do ATR", min=2, max=200),
        ParamSpec("atr_multiplier", "number", atr_multiplier, "Distância do stop em ATRs", min=0.1, max=20),
        ParamSpec(
            "take_profit_r",
            "number",
            take_profit_r,
            "Take profit em múltiplos do risco (R); 0 = desliga",
            min=0,
            max=20,
        ),
        ParamSpec("allow_short", "boolean", True, "Permitir posições vendidas"),
        ParamSpec("risk_fraction", "number", 1.0, "Fração do risco máximo por trade usada", min=0.01, max=1),
    )


def trail_param(default: float) -> ParamSpec:
    return ParamSpec("trail_atr", "number", default, "Trailing stop em ATRs (0 = desliga)", min=0, max=20)


def trend_filter_params(period: int = 200, enabled: bool = True) -> tuple[ParamSpec, ...]:
    return (
        ParamSpec("use_trend_filter", "boolean", enabled, "Só opera a favor da média de tendência"),
        ParamSpec("trend_period", "integer", period, "Período da média de tendência", min=10, max=500),
    )


class ClassicStrategy(AtrStrategy):
    """AtrStrategy + structural stops, take profits and bookkeeping of the entry candle (time exits)."""

    category: ClassVar[str] = ""

    def _open(
        self,
        ctx: StrategyContext,
        side: OrderSide,
        atr_v: float,
        tag: str,
        *,
        stop: float | None = None,
        take_profit: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> list[OrderIntent]:
        """Entry with a stop (structural `stop` with an ATR floor, else ATR × atr_multiplier) and optional TP."""
        if side == OrderSide.SELL and not self.params["allow_short"]:
            return []
        price = float(ctx.price)
        sign = 1 if side == OrderSide.BUY else -1
        if stop is None:
            distance = atr_v * self.params["atr_multiplier"]
        else:
            distance = (price - stop) * sign  # positive when the stop is on the protective side
            distance = max(distance, MIN_STOP_ATR * atr_v)
        stop_px = price - sign * distance
        if distance <= 0 or stop_px <= 0:
            return []
        if take_profit is None and self.params.get("take_profit_r"):
            take_profit = price + sign * distance * self.params["take_profit_r"]
        tp: Decimal | None = None
        if take_profit is not None and take_profit > 0 and (take_profit - price) * sign > 0:
            tp = to_decimal(round(take_profit, 10))
        return [
            OrderIntent(
                side,
                None,
                stop_loss=to_decimal(round(stop_px, 10)),
                take_profit=tp,
                tag=tag,
                risk_fraction=to_decimal(self.params["risk_fraction"]),
                meta={"atr": round(atr_v, 8), **(meta or {})},
            )
        ]

    def _flip(self, ctx: StrategyContext, side: OrderSide, atr_v: float, tag: str, **kw: Any) -> list[OrderIntent]:
        """Close an opposite position and open `side` (if allowed) in the same candle."""
        pos = ctx.position_qty
        out: list[OrderIntent] = []
        if (side == OrderSide.BUY and pos < 0) or (side == OrderSide.SELL and pos > 0):
            out += self._exit(ctx, f"{tag}_reverse")
            pos = Decimal(0)
        if pos == 0:
            out += self._open(ctx, side, atr_v, tag, **kw)
        return out

    @staticmethod
    def _trend_ok(params: dict[str, Any], closes: list[float], side: OrderSide) -> bool | None:
        """True/False for the trend filter, None while the trend average is warming up."""
        if not params.get("use_trend_filter", False):
            return True
        t = (
            sma(closes, params["trend_period"])[-1]
            if params.get("trend_sma")
            else ema(closes, params["trend_period"])[-1]
        )
        if t is None:
            return None
        return closes[-1] > t if side == OrderSide.BUY else closes[-1] < t

    def on_result(self, intent: OrderIntent, filled: bool, ctx: StrategyContext) -> None:
        if not filled:
            return
        if intent.reduce_only:
            ctx.state.pop("entry_time", None)
        else:
            ctx.state["entry_time"] = ctx.candles[-1].open_time

    def on_stop_out(self, ctx: StrategyContext) -> None:
        super().on_stop_out(ctx)
        ctx.state.pop("entry_time", None)

    @staticmethod
    def _bars_held(ctx: StrategyContext) -> int | None:
        t = ctx.state.get("entry_time")
        if t is None or ctx.position_qty == 0:
            return None
        return sum(1 for c in ctx.candles if c.open_time > int(t))

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {**super().describe(), "category": cls.category}


# ---------------------------------------------------------------------------------------------------- trend
class MacdTrendStrategy(ClassicStrategy):
    key = "macd_trend"
    name = "MACD + sinal com filtro de tendência (EMA 200)"
    category = "tendência"
    description = (
        "Compra quando o MACD cruza acima da linha de sinal com o preço acima da EMA de tendência (vende no "
        "inverso). Sai no cruzamento oposto. Clássico de Gerald Appel."
    )
    trailing = True
    params_spec = (
        ParamSpec("fast", "integer", 12, "EMA rápida do MACD", min=2, max=200),
        ParamSpec("slow", "integer", 26, "EMA lenta do MACD", min=3, max=400),
        ParamSpec("signal", "integer", 9, "Linha de sinal", min=2, max=100),
        *trend_filter_params(200),
        trail_param(0.0),
        *base_params(2.5),
    )

    @classmethod
    def check_params(cls, params: dict[str, Any]) -> None:
        if params["fast"] >= params["slow"]:
            raise StrategyParamError("fast must be smaller than slow")

    @property
    def warmup(self) -> int:
        p = self.params
        return max(p["slow"] + p["signal"], p["trend_period"] if p["use_trend_filter"] else 0, p["atr_period"]) + 3

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        closes = ctx.closes
        line, sig, _ = macd(closes, self.params["fast"], self.params["slow"], self.params["signal"])
        pos = ctx.position_qty
        if crossed_above(line, sig):
            if self._trend_ok(self.params, closes, OrderSide.BUY):
                return self._flip(ctx, OrderSide.BUY, atr_v, "macd_long")
            return self._exit(ctx, "macd_exit_short") if pos < 0 else []
        if crossed_below(line, sig):
            if self._trend_ok(self.params, closes, OrderSide.SELL) and self.params["allow_short"]:
                return self._flip(ctx, OrderSide.SELL, atr_v, "macd_short")
            return self._exit(ctx, "macd_exit_long") if pos > 0 else []
        return []


class TurtleStrategy(ClassicStrategy):
    key = "turtle"
    name = "Turtle Trading (Donchian 20/55, stop 2N)"
    category = "rompimento"
    description = (
        "Regras dos Turtles (Dennis/Eckhardt): entra no rompimento da máxima/mínima de 20 candles (sistema 1) — "
        "pulando o sinal se o último rompimento foi vencedor, exceto no rompimento de 55 (failsafe). Stop em 2N "
        "(N = ATR 20), saída no canal oposto de 10. Tamanho pelo risco até o stop (equivalente à unidade N)."
    )
    params_spec = (
        ParamSpec("entry_period", "integer", 20, "Canal de entrada (sistema 1)", min=5, max=300),
        ParamSpec("failsafe_period", "integer", 55, "Canal de entrada failsafe (sistema 2)", min=10, max=500),
        ParamSpec("exit_period", "integer", 10, "Canal de saída", min=2, max=200),
        ParamSpec("skip_after_win", "boolean", True, "Pula o rompimento de 20 após um trade vencedor"),
        *base_params(2.0, 0.0, atr_period=20),
    )

    @classmethod
    def check_params(cls, params: dict[str, Any]) -> None:
        if not params["exit_period"] < params["entry_period"] <= params["failsafe_period"]:
            raise StrategyParamError("exit_period < entry_period <= failsafe_period")

    @property
    def warmup(self) -> int:
        return max(self.params["failsafe_period"], self.params["atr_period"]) + 2

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        c = ctx.candles
        highs, lows, close = [x.high for x in c[:-1]], [x.low for x in c[:-1]], ctx.closes[-1]
        p, pos, st = self.params, ctx.position_qty, ctx.state
        up20, _, lo20 = donchian(highs, lows, p["entry_period"])
        up55, _, lo55 = donchian(highs, lows, p["failsafe_period"])
        xu, _, xl = donchian(highs, lows, p["exit_period"])
        if None in (up20[-1], up55[-1], xu[-1]):
            return []
        entry = float(ctx.position_entry or 0)
        if pos > 0 and close < xl[-1]:  # type: ignore[operator]
            st["last_win"] = close > entry
            return self._exit(ctx, "turtle_exit_long")
        if pos < 0 and close > xu[-1]:  # type: ignore[operator]
            st["last_win"] = close < entry
            return self._exit(ctx, "turtle_exit_short")
        if pos != 0:
            return []
        long20, long55 = close > up20[-1], close > up55[-1]  # type: ignore[operator]
        short20, short55 = close < lo20[-1], close < lo55[-1]  # type: ignore[operator]
        if not (long20 or short20):
            return []
        failsafe = long55 if long20 else short55
        if p["skip_after_win"] and st.get("last_win") and not failsafe:
            st["last_win"] = None  # simplified: the skipped breakout "resets" the filter
            return []
        side = OrderSide.BUY if long20 else OrderSide.SELL
        return self._open(
            ctx, side, atr_v, f"turtle_{'55' if failsafe else '20'}_{side.value.lower()}", meta={"n": atr_v}
        )

    def on_stop_out(self, ctx: StrategyContext) -> None:
        super().on_stop_out(ctx)
        ctx.state["last_win"] = False


class TripleEmaStrategy(ClassicStrategy):
    key = "triple_ema"
    name = "Fita de 3 EMAs (8/21/55)"
    category = "tendência"
    description = (
        "Entra quando as três EMAs se alinham (rápida > média > lenta para compra) e o preço fecha acima da "
        "rápida; sai quando a rápida cruza a média ou no trailing stop por ATR."
    )
    trailing = True
    params_spec = (
        ParamSpec("fast", "integer", 8, "EMA rápida", min=2, max=200),
        ParamSpec("mid", "integer", 21, "EMA média", min=3, max=300),
        ParamSpec("slow", "integer", 55, "EMA lenta", min=4, max=500),
        trail_param(3.0),
        *base_params(2.0),
    )

    @classmethod
    def check_params(cls, params: dict[str, Any]) -> None:
        if not params["fast"] < params["mid"] < params["slow"]:
            raise StrategyParamError("fast < mid < slow")

    @property
    def warmup(self) -> int:
        return max(self.params["slow"], self.params["atr_period"]) + 3

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        closes = ctx.closes
        f, m, s = (ema(closes, self.params[k]) for k in ("fast", "mid", "slow"))
        if None in (f[-2], m[-2], s[-2]):
            return []
        pos = ctx.position_qty
        if pos > 0 and f[-1] < m[-1]:  # type: ignore[operator]
            return self._exit(ctx, "tema_exit_long")
        if pos < 0 and f[-1] > m[-1]:  # type: ignore[operator]
            return self._exit(ctx, "tema_exit_short")
        if pos != 0:
            return []
        bull = lambda i: f[i] > m[i] > s[i]  # type: ignore[operator]  # noqa: E731
        bear = lambda i: f[i] < m[i] < s[i]  # type: ignore[operator]  # noqa: E731
        if bull(-1) and not bull(-2) and closes[-1] > f[-1]:  # type: ignore[operator]
            return self._open(ctx, OrderSide.BUY, atr_v, "tema_long")
        if bear(-1) and not bear(-2) and closes[-1] < f[-1]:  # type: ignore[operator]
            return self._open(ctx, OrderSide.SELL, atr_v, "tema_short")
        return []


class HeikinAshiEmaStrategy(ClassicStrategy):
    key = "heikin_ashi_ema"
    name = "Heikin-Ashi + EMA de tendência"
    category = "tendência"
    description = (
        "Compra quando o candle Heikin-Ashi vira de baixa para alta (corpo forte, sem sombra inferior opcional) "
        "acima da EMA; vende no inverso. Sai quando o Heikin-Ashi muda de cor."
    )
    params_spec = (
        ParamSpec("ema_period", "integer", 50, "EMA de tendência", min=5, max=400),
        ParamSpec("require_no_wick", "boolean", False, "Exige candle HA sem sombra contra o movimento"),
        *base_params(2.0),
    )

    @property
    def warmup(self) -> int:
        return max(self.params["ema_period"], self.params["atr_period"]) + 10

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        c = ctx.candles
        ho, hh, hl, hc = heikin_ashi([x.open for x in c], [x.high for x in c], [x.low for x in c], ctx.closes)
        e = ema(ctx.closes, self.params["ema_period"])[-1]
        if e is None:
            return []
        green_now, green_prev = hc[-1] > ho[-1], hc[-2] > ho[-2]
        red_now, red_prev = hc[-1] < ho[-1], hc[-2] < ho[-2]
        pos, close = ctx.position_qty, ctx.closes[-1]
        if pos > 0 and red_now:
            return self._exit(ctx, "ha_exit_long")
        if pos < 0 and green_now:
            return self._exit(ctx, "ha_exit_short")
        if pos != 0:
            return []
        tol = 1e-9 * max(abs(hc[-1]), 1.0)
        strict = self.params["require_no_wick"]
        if green_now and red_prev and close > e and (not strict or hl[-1] >= min(ho[-1], hc[-1]) - tol):
            return self._open(ctx, OrderSide.BUY, atr_v, "ha_long")
        if red_now and green_prev and close < e and (not strict or hh[-1] <= max(ho[-1], hc[-1]) + tol):
            return self._open(ctx, OrderSide.SELL, atr_v, "ha_short")
        return []


class IchimokuBreakoutStrategy(ClassicStrategy):
    key = "ichimoku_breakout"
    name = "Ichimoku: rompimento da nuvem"
    category = "tendência"
    description = (
        "Compra quando o fechamento rompe acima da nuvem (Kumo) com Tenkan > Kijun e Chikou acima do preço de 26 "
        "candles atrás (vende no inverso). Sai no fechamento abaixo/acima da Kijun."
    )
    params_spec = (
        ParamSpec("tenkan", "integer", 9, "Tenkan-sen", min=2, max=100),
        ParamSpec("kijun", "integer", 26, "Kijun-sen", min=3, max=200),
        ParamSpec("senkou_b", "integer", 52, "Senkou Span B", min=4, max=400),
        ParamSpec("displacement", "integer", 26, "Deslocamento da nuvem / Chikou", min=1, max=200),
        *base_params(2.5),
    )

    @classmethod
    def check_params(cls, params: dict[str, Any]) -> None:
        if not params["tenkan"] < params["kijun"] < params["senkou_b"]:
            raise StrategyParamError("tenkan < kijun < senkou_b")

    @property
    def warmup(self) -> int:
        return self.params["senkou_b"] + self.params["displacement"] + 3

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        c, closes, p = ctx.candles, ctx.closes, self.params
        ich = ichimoku(
            [x.high for x in c], [x.low for x in c], p["tenkan"], p["kijun"], p["senkou_b"], p["displacement"]
        )
        t, k, a, b = ich["tenkan"], ich["kijun"], ich["span_a"], ich["span_b"]
        if None in (t[-1], k[-1], a[-1], b[-1], a[-2], b[-2]) or len(closes) <= p["displacement"] + 1:
            return []
        pos, close = ctx.position_qty, closes[-1]
        if pos > 0 and close < k[-1]:  # type: ignore[operator]
            return self._exit(ctx, "ichimoku_exit_long")
        if pos < 0 and close > k[-1]:  # type: ignore[operator]
            return self._exit(ctx, "ichimoku_exit_short")
        if pos != 0:
            return []
        top, top_prev = max(a[-1], b[-1]), max(a[-2], b[-2])  # type: ignore[type-var]
        bot, bot_prev = min(a[-1], b[-1]), min(a[-2], b[-2])  # type: ignore[type-var]
        lagging = closes[-1 - p["displacement"]]
        if close > top and closes[-2] <= top_prev and t[-1] > k[-1] and close > lagging:  # type: ignore[operator]
            return self._open(ctx, OrderSide.BUY, atr_v, "ichimoku_long")
        if close < bot and closes[-2] >= bot_prev and t[-1] < k[-1] and close < lagging:  # type: ignore[operator]
            return self._open(ctx, OrderSide.SELL, atr_v, "ichimoku_short")
        return []


class HolyGrailStrategy(ClassicStrategy):
    key = "holy_grail"
    name = "Holy Grail (Linda Raschke): ADX > 30 + pullback à EMA 20"
    category = "tendência"
    description = (
        "Com ADX acima de 30 (tendência forte) e +DI > −DI, espera o preço recuar até a EMA 20 e entra quando o "
        "candle seguinte fecha acima da máxima do candle do pullback (vende no inverso). Stop estrutural abaixo da "
        "mínima do pullback; alvo na máxima recente (swing) ou em R."
    )
    trailing = True
    params_spec = (
        ParamSpec("adx_period", "integer", 14, "Período do ADX", min=2, max=100),
        ParamSpec("adx_threshold", "number", 30.0, "ADX mínimo", min=10, max=80),
        ParamSpec("ema_period", "integer", 20, "EMA do pullback", min=5, max=200),
        ParamSpec(
            "swing_lookback", "integer", 20, "Candles para o alvo na máxima/mínima recente (0 = só R)", min=0, max=200
        ),
        ParamSpec("stop_buffer_atr", "number", 0.25, "Folga do stop além da mínima do pullback (ATRs)", min=0, max=5),
        trail_param(3.0),
        *base_params(2.0, 2.0),
    )

    @property
    def warmup(self) -> int:
        p = self.params
        return max(2 * p["adx_period"], p["ema_period"], p["swing_lookback"], p["atr_period"]) + 5

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        if ctx.position_qty != 0:
            return []  # exits: protective stop, take profit and the ATR trailing stop
        c, closes, p = ctx.candles, ctx.closes, self.params
        highs, lows = [x.high for x in c], [x.low for x in c]
        a, pdi, mdi = adx(highs, lows, closes, p["adx_period"])
        e = ema(closes, p["ema_period"])
        if None in (a[-2], pdi[-2], mdi[-2], e[-1], e[-2], e[-3]) or a[-2] < p["adx_threshold"]:  # type: ignore[operator]
            return []
        buf = p["stop_buffer_atr"] * atr_v
        n = p["swing_lookback"]
        if pdi[-2] > mdi[-2] and closes[-3] > e[-3] and lows[-2] <= e[-2] and closes[-1] > highs[-2]:  # type: ignore[operator]
            target = max(highs[-n - 1 : -1]) if n else None
            return self._open(
                ctx,
                OrderSide.BUY,
                atr_v,
                "holy_grail_long",
                stop=min(lows[-2], lows[-1]) - buf,
                take_profit=target if target and target > closes[-1] else None,
            )
        if mdi[-2] > pdi[-2] and closes[-3] < e[-3] and highs[-2] >= e[-2] and closes[-1] < lows[-2]:  # type: ignore[operator]
            target = min(lows[-n - 1 : -1]) if n else None
            return self._open(
                ctx,
                OrderSide.SELL,
                atr_v,
                "holy_grail_short",
                stop=max(highs[-2], highs[-1]) + buf,
                take_profit=target if target and target < closes[-1] else None,
            )
        return []


# ---------------------------------------------------------------------------------------------------- breakout
class KeltnerBreakoutStrategy(ClassicStrategy):
    key = "keltner_breakout"
    name = "Rompimento do canal de Keltner"
    category = "rompimento"
    description = (
        "Entra quando o fechamento rompe a banda superior/inferior do canal de Keltner (EMA ± k × ATR); "
        "sai quando o preço volta à EMA central ou no trailing stop por ATR."
    )
    trailing = True
    params_spec = (
        ParamSpec("kc_period", "integer", 20, "EMA central", min=5, max=300),
        ParamSpec("kc_atr_period", "integer", 10, "ATR do canal", min=2, max=200),
        ParamSpec("kc_multiplier", "number", 2.0, "Largura do canal em ATRs", min=0.5, max=6),
        trail_param(2.5),
        *base_params(2.0),
    )

    @property
    def warmup(self) -> int:
        p = self.params
        return max(p["kc_period"], p["kc_atr_period"], p["atr_period"]) + 3

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        c, closes, p = ctx.candles, ctx.closes, self.params
        mid, up, lo = keltner(
            [x.high for x in c], [x.low for x in c], closes, p["kc_period"], p["kc_atr_period"], p["kc_multiplier"]
        )
        if None in (mid[-1], up[-1], lo[-1], up[-2], lo[-2]):
            return []
        pos, close = ctx.position_qty, closes[-1]
        if pos > 0 and close < mid[-1]:  # type: ignore[operator]
            return self._exit(ctx, "keltner_exit_long")
        if pos < 0 and close > mid[-1]:  # type: ignore[operator]
            return self._exit(ctx, "keltner_exit_short")
        if pos != 0:
            return []
        if close > up[-1] and closes[-2] <= up[-2]:  # type: ignore[operator]
            return self._open(ctx, OrderSide.BUY, atr_v, "keltner_long")
        if close < lo[-1] and closes[-2] >= lo[-2]:  # type: ignore[operator]
            return self._open(ctx, OrderSide.SELL, atr_v, "keltner_short")
        return []


class VolatilityBreakoutStrategy(ClassicStrategy):
    key = "volatility_breakout"
    name = "Volatility breakout (Larry Williams)"
    category = "rompimento"
    description = (
        "Compra quando o candle fecha acima de abertura + k × amplitude do candle anterior (vende abaixo de "
        "abertura − k × amplitude) e sai após N candles (saída por tempo), no stop ou no alvo. Ideia de Larry "
        "Williams; pensado para 4h/1d."
    )
    params_spec = (
        ParamSpec("k", "number", 0.6, "Fração da amplitude anterior", min=0.1, max=3),
        ParamSpec("hold_bars", "integer", 1, "Candles mantidos antes da saída por tempo", min=1, max=50),
        *trend_filter_params(50, True),
        *base_params(2.0),
    )

    @property
    def warmup(self) -> int:
        p = self.params
        return max(p["trend_period"] if p["use_trend_filter"] else 0, p["atr_period"]) + 3

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        pos = ctx.position_qty
        if pos != 0:
            held = self._bars_held(ctx)
            if held is not None and held >= self.params["hold_bars"]:
                return self._exit(ctx, "vbo_time_exit")
            return []
        prev, cur = ctx.candles[-2], ctx.candles[-1]
        rng = (prev.high - prev.low) * self.params["k"]
        if rng <= 0:
            return []
        if cur.close > cur.open + rng and self._trend_ok(self.params, ctx.closes, OrderSide.BUY):
            return self._open(ctx, OrderSide.BUY, atr_v, "vbo_long")
        if cur.close < cur.open - rng and self._trend_ok(self.params, ctx.closes, OrderSide.SELL):
            return self._open(ctx, OrderSide.SELL, atr_v, "vbo_short")
        return []


class SessionBreakoutStrategy(ClassicStrategy):
    key = "session_breakout"
    name = "Rompimento da faixa de abertura (sessão UTC)"
    category = "rompimento"
    description = (
        "Opening range breakout: mede a máxima/mínima dos primeiros minutos de uma sessão UTC (padrão 13:00, "
        "abertura de Nova York) e entra no primeiro rompimento; stop no lado oposto da faixa, alvo em R e "
        "encerramento no fim da sessão. Requer timeframe intradiário menor que a faixa (ex.: 5m/15m)."
    )
    params_spec = (
        ParamSpec("session_start_hour", "integer", 13, "Hora UTC de início da sessão", min=0, max=23),
        ParamSpec("range_minutes", "integer", 60, "Duração da faixa de abertura (min)", min=5, max=480),
        ParamSpec("session_hours", "integer", 8, "Duração da sessão (horas); posição é fechada no fim", min=1, max=24),
        ParamSpec("stop_at_mid", "boolean", False, "Stop no meio da faixa (em vez do lado oposto)"),
        *base_params(1.5, 2.0),
    )

    @classmethod
    def check_params(cls, params: dict[str, Any]) -> None:
        if params["range_minutes"] >= params["session_hours"] * 60:
            raise StrategyParamError("range_minutes must be shorter than the session")

    @property
    def warmup(self) -> int:
        return self.params["atr_period"] + 3

    def _window(self, t: int) -> tuple[int, int, int]:
        start_ms = self.params["session_start_hour"] * HOUR_MS
        anchor = (t - start_ms) // DAY_MS * DAY_MS + start_ms
        return anchor, anchor + self.params["range_minutes"] * 60_000, anchor + self.params["session_hours"] * HOUR_MS

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        last = ctx.candles[-1]
        bar = last.close_time + 1 - last.open_time
        anchor, range_end, session_end = self._window(last.open_time)
        pos = ctx.position_qty
        if pos != 0:
            entry_t = ctx.state.get("entry_time")
            stale = entry_t is not None and self._window(int(entry_t))[0] != anchor
            if last.close_time + 1 >= session_end or last.open_time >= session_end or stale:
                return self._exit(ctx, "orb_session_end")
            return []
        if bar <= 0 or bar > range_end - anchor or not (range_end <= last.open_time < session_end):
            return []
        if ctx.state.get("traded_session") == anchor:
            return []  # one trade per session
        rng = [x for x in ctx.candles if anchor <= x.open_time and x.close_time < range_end]
        if len(rng) * bar < range_end - anchor:
            return []  # incomplete opening range (missing candles)
        hi, lo = max(x.high for x in rng), min(x.low for x in rng)
        mid = (hi + lo) / 2
        if last.close > hi:
            return self._open(
                ctx,
                OrderSide.BUY,
                atr_v,
                "orb_long",
                stop=mid if self.params["stop_at_mid"] else lo,
                meta={"range_high": hi, "range_low": lo},
            )
        if last.close < lo:
            return self._open(
                ctx,
                OrderSide.SELL,
                atr_v,
                "orb_short",
                stop=mid if self.params["stop_at_mid"] else hi,
                meta={"range_high": hi, "range_low": lo},
            )
        return []

    def on_result(self, intent: OrderIntent, filled: bool, ctx: StrategyContext) -> None:
        super().on_result(intent, filled, ctx)
        if filled and not intent.reduce_only:
            ctx.state["traded_session"] = self._window(ctx.candles[-1].open_time)[0]


# ---------------------------------------------------------------------------------------------------- reversion
class ConnorsRsi2Strategy(ClassicStrategy):
    key = "connors_rsi2"
    name = "RSI-2 de Larry Connors (filtro SMA 200)"
    category = "reversão"
    description = (
        "Compra recuos curtos em tendência de alta: preço acima da SMA 200 e RSI(2) abaixo de 10; sai quando o "
        "fechamento supera a SMA 5 (vende no inverso). Stop largo por ATR (o original não usa stop) e saída por "
        "tempo opcional."
    )
    params_spec = (
        ParamSpec("rsi_period", "integer", 2, "Período do RSI", min=2, max=10),
        ParamSpec("oversold", "number", 10.0, "RSI de entrada comprada", min=1, max=40),
        ParamSpec("overbought", "number", 90.0, "RSI de entrada vendida", min=60, max=99),
        ParamSpec("trend_period", "integer", 200, "SMA de tendência", min=20, max=500),
        ParamSpec("exit_period", "integer", 5, "SMA de saída", min=2, max=50),
        ParamSpec("max_bars", "integer", 10, "Saída por tempo em candles (0 = desliga)", min=0, max=200),
        *base_params(3.0),
    )

    @property
    def warmup(self) -> int:
        p = self.params
        return max(p["trend_period"], p["atr_period"], p["rsi_period"] + 2) + 2

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        closes, p, pos = ctx.closes, self.params, ctx.position_qty
        r = rsi(closes, p["rsi_period"])[-1]
        trend, exit_ma = sma(closes, p["trend_period"])[-1], sma(closes, p["exit_period"])[-1]
        if None in (r, trend, exit_ma):
            return []
        close = closes[-1]
        held = self._bars_held(ctx)
        if pos != 0 and p["max_bars"] and held is not None and held >= p["max_bars"]:
            return self._exit(ctx, "rsi2_time_exit")
        if pos > 0 and close > exit_ma:  # type: ignore[operator]
            return self._exit(ctx, "rsi2_exit_long")
        if pos < 0 and close < exit_ma:  # type: ignore[operator]
            return self._exit(ctx, "rsi2_exit_short")
        if pos != 0:
            return []
        if close > trend and r < p["oversold"]:  # type: ignore[operator]
            return self._open(ctx, OrderSide.BUY, atr_v, "rsi2_long", meta={"rsi": round(r, 2)})  # type: ignore[arg-type]
        if close < trend and r > p["overbought"]:  # type: ignore[operator]
            return self._open(ctx, OrderSide.SELL, atr_v, "rsi2_short", meta={"rsi": round(r, 2)})  # type: ignore[arg-type]
        return []


class StochRsiReversalStrategy(ClassicStrategy):
    key = "stoch_rsi_reversal"
    name = "Stochastic RSI: reversão nos extremos"
    category = "reversão"
    description = (
        "Compra quando o %K do Stochastic RSI cruza acima do %D na zona de sobrevenda (vende na sobrecompra), "
        "opcionalmente só a favor da EMA de tendência; sai quando o %K chega ao extremo oposto."
    )
    params_spec = (
        ParamSpec("rsi_period", "integer", 14, "Período do RSI", min=2, max=100),
        ParamSpec("stoch_period", "integer", 14, "Período do estocástico", min=2, max=100),
        ParamSpec("k_smooth", "integer", 3, "Suavização %K", min=1, max=20),
        ParamSpec("d_smooth", "integer", 3, "Suavização %D", min=1, max=20),
        ParamSpec("oversold", "number", 20.0, "Sobrevenda", min=1, max=49),
        ParamSpec("overbought", "number", 80.0, "Sobrecompra", min=51, max=99),
        *trend_filter_params(200, True),
        *base_params(2.0),
    )

    @property
    def warmup(self) -> int:
        p = self.params
        core = p["rsi_period"] + p["stoch_period"] + p["k_smooth"] + p["d_smooth"]
        return max(core, p["trend_period"] if p["use_trend_filter"] else 0, p["atr_period"]) + 3

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        p, pos, closes = self.params, ctx.position_qty, ctx.closes
        k, d = stoch_rsi(closes, p["rsi_period"], p["stoch_period"], p["k_smooth"], p["d_smooth"])
        if None in (k[-1], k[-2], d[-1], d[-2]):
            return []
        if pos > 0 and k[-1] >= p["overbought"]:  # type: ignore[operator]
            return self._exit(ctx, "stochrsi_exit_long")
        if pos < 0 and k[-1] <= p["oversold"]:  # type: ignore[operator]
            return self._exit(ctx, "stochrsi_exit_short")
        if pos != 0:
            return []
        if crossed_above(k, d) and k[-2] < p["oversold"] and self._trend_ok(p, closes, OrderSide.BUY):  # type: ignore[operator]
            return self._open(ctx, OrderSide.BUY, atr_v, "stochrsi_long")
        if crossed_below(k, d) and k[-2] > p["overbought"] and self._trend_ok(p, closes, OrderSide.SELL):  # type: ignore[operator]
            return self._open(ctx, OrderSide.SELL, atr_v, "stochrsi_short")
        return []


class VwapReversionStrategy(ClassicStrategy):
    key = "vwap_reversion"
    name = "Reversão à VWAP intradiária (experimental)"
    category = "reversão"
    description = (
        "VWAP ancorada no início do dia UTC com bandas de desvio-padrão ponderado por volume: compra quando o preço "
        "fecha de volta para dentro da banda inferior (vende na superior), alvo na VWAP, e fecha tudo no fim do dia "
        "UTC. Só para timeframes intradiários (1m–1h)."
    )
    params_spec = (
        ParamSpec("band_std", "number", 2.0, "Desvios-padrão das bandas", min=0.5, max=5),
        ParamSpec("min_session_bars", "integer", 6, "Candles mínimos na sessão antes de operar", min=2, max=500),
        ParamSpec("flat_at_session_end", "boolean", True, "Fecha a posição no último candle do dia UTC"),
        *base_params(1.5),
    )

    @property
    def warmup(self) -> int:
        return max(self.params["atr_period"], self.params["min_session_bars"]) + 3

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        c, p, pos = ctx.candles, self.params, ctx.position_qty
        last = c[-1]
        if last.close_time + 1 - last.open_time >= DAY_MS:
            return []  # daily candles: VWAP of a single candle is meaningless
        if pos != 0 and p["flat_at_session_end"] and (last.close_time + 1) % DAY_MS == 0:
            return self._exit(ctx, "vwap_session_end")
        vwap, sd = session_vwap(
            [x.open_time for x in c], [x.high for x in c], [x.low for x in c], ctx.closes, [x.volume for x in c]
        )
        if None in (vwap[-1], vwap[-2], sd[-1], sd[-2]):
            return []
        close = last.close
        if pos > 0 and close >= vwap[-1]:  # type: ignore[operator]
            return self._exit(ctx, "vwap_exit_long")
        if pos < 0 and close <= vwap[-1]:  # type: ignore[operator]
            return self._exit(ctx, "vwap_exit_short")
        if pos != 0:
            return []
        day = last.open_time // DAY_MS
        bars_today = sum(1 for x in c if x.open_time // DAY_MS == day)
        if bars_today < p["min_session_bars"] or c[-2].open_time // DAY_MS != day:
            return []
        k = p["band_std"]
        lo_prev, lo = vwap[-2] - k * sd[-2], vwap[-1] - k * sd[-1]  # type: ignore[operator]
        hi_prev, hi = vwap[-2] + k * sd[-2], vwap[-1] + k * sd[-1]  # type: ignore[operator]
        if c[-2].close < lo_prev and close >= lo and close < vwap[-1]:  # type: ignore[operator]
            return self._open(ctx, OrderSide.BUY, atr_v, "vwap_long", take_profit=vwap[-1])
        if c[-2].close > hi_prev and close <= hi and close > vwap[-1]:  # type: ignore[operator]
            return self._open(ctx, OrderSide.SELL, atr_v, "vwap_short", take_profit=vwap[-1])
        return []


CLASSIC_STRATEGIES: tuple[type[ClassicStrategy], ...] = (
    MacdTrendStrategy,
    TurtleStrategy,
    TripleEmaStrategy,
    HeikinAshiEmaStrategy,
    IchimokuBreakoutStrategy,
    HolyGrailStrategy,
    KeltnerBreakoutStrategy,
    VolatilityBreakoutStrategy,
    SessionBreakoutStrategy,
    ConnorsRsi2Strategy,
    StochRsiReversalStrategy,
    VwapReversionStrategy,
)
CLASSIC_STRATEGY_KEYS: tuple[str, ...] = tuple(cls.key for cls in CLASSIC_STRATEGIES)
