"""Funding-rate contrarian strategy (F-14).

Perpetual futures pay funding every 8 h. A persistently **extreme positive** rate means longs are crowded and
paying a premium to stay long (often near local tops); an **extreme negative** rate means crowded shorts (often
near local bottoms, short squeezes). This strategy fades the crowd:

* entry: the mean of the last ``avg_events`` settled rates is beyond ``entry_rate`` (absolute, per 8 h) **and**
  (if ``z_entry`` > 0) at least ``z_entry`` standard deviations away from the mean of the last
  ``lookback_events`` rates **and** (if ``confirm_ema`` > 0) the price already turned (short: close below the EMA;
  long: above). Positive extreme -> short; negative extreme -> long. At most one entry per settled funding event.
* exit: funding normalized (``|rate| <= exit_rate`` or flipped sign), ``max_hold_bars`` reached, the ATR stop
  (exchange-side) or the optional take profit at ``take_profit_rr`` × stop distance.

The strategy only reads *settled* rates (``ctx.funding``), the same data in bots (exchange history fetched each
new candle) and in the backtest (history/archive aligned to the candle close: no look-ahead). Without funding data
it never trades. While holding against the crowd the position *receives* funding, which the backtest accounts for.
"""

from __future__ import annotations

from typing import Any, ClassVar

from app.models.enums import OrderSide
from app.strategy.base import OrderIntent, ParamSpec, StrategyContext, StrategyParamError, to_decimal
from app.strategy.indicators import ema
from app.strategy.technical import COMMON_PARAMS, AtrStrategy


class FundingContrarianStrategy(AtrStrategy):
    key = "funding_contrarian"
    name = "Funding contrarian (reversão de funding extremo)"
    description = (
        "Vende quando o funding fica extremamente positivo (comprados lotados) e compra quando fica extremamente "
        "negativo (vendidos lotados), com confirmação de preço pela EMA e stop ATR; sai quando o funding normaliza."
    )
    needs_funding: ClassVar[bool] = True
    params_spec = (
        ParamSpec(
            "entry_rate", "number", 0.0005, "Funding de entrada (fração por 8 h; 0.0005 = 0,05%)", min=0, max=0.01
        ),
        ParamSpec("exit_rate", "number", 0.0001, "Funding de saída (normalizado)", min=0, max=0.01),
        ParamSpec("z_entry", "number", 1.5, "Desvios-padrão acima da média recente (0 = ignorar)", min=0, max=10),
        ParamSpec(
            "lookback_events", "integer", 30, "Eventos de funding para média/desvio (30 = 10 dias)", min=5, max=500
        ),  # fmt: skip
        ParamSpec("avg_events", "integer", 1, "Média dos últimos N fundings usada no sinal", min=1, max=9),
        ParamSpec("confirm_ema", "integer", 20, "EMA de confirmação do preço (0 = sem confirmação)", min=0, max=400),
        ParamSpec(
            "take_profit_rr", "number", 2.0, "Alvo em múltiplos da distância do stop (0 = sem alvo)", min=0, max=20
        ),  # fmt: skip
        ParamSpec("max_hold_bars", "integer", 72, "Saída por tempo em candles (0 = sem limite)", min=0, max=10_000),
        *COMMON_PARAMS,
    )

    @classmethod
    def check_params(cls, params: dict[str, Any]) -> None:
        if params["entry_rate"] <= 0:
            raise StrategyParamError("entry_rate deve ser > 0")
        if params["exit_rate"] >= params["entry_rate"]:
            raise StrategyParamError("exit_rate deve ser menor que entry_rate")

    @property
    def warmup(self) -> int:
        return max(self.params["atr_period"], self.params["confirm_ema"]) + 3

    @property
    def funding_lookback(self) -> int:
        return self.params["lookback_events"] + self.params["avg_events"]

    # ------------------------------------------------------------------ #
    def _signal_rate(self, ctx: StrategyContext) -> tuple[float, float | None, int] | None:
        events = ctx.funding
        n = self.params["avg_events"]
        if len(events) < n:
            return None
        recent = sum(float(e.rate) for e in events[-n:]) / n
        hist = [float(e.rate) for e in events[-self.params["lookback_events"] :]]
        z: float | None = None
        if len(hist) >= 5:
            mean = sum(hist) / len(hist)
            var = sum((x - mean) ** 2 for x in hist) / (len(hist) - 1)
            sd = var**0.5
            z = (recent - mean) / sd if sd > 0 else 0.0
        return recent, z, events[-1].time

    def signal(self, ctx: StrategyContext, atr_v: float) -> list[OrderIntent]:
        sig = self._signal_rate(ctx)
        pos = ctx.position_qty
        if pos != 0:
            return self._manage(ctx, sig)
        ctx.state.pop("held", None)
        if sig is None:
            return []
        recent, z, event_time = sig
        if ctx.state.get("entered_on") == event_time:
            return []  # one entry per settled funding event (no re-entry right after a stop-out)
        p = self.params
        z_ok = p["z_entry"] <= 0 or (z is not None and abs(z) >= p["z_entry"] and (z > 0) == (recent > 0))
        if abs(recent) < p["entry_rate"] or not z_ok:
            return []
        side = OrderSide.SELL if recent > 0 else OrderSide.BUY
        if p["confirm_ema"]:
            line = ema(ctx.closes, p["confirm_ema"])[-1]
            if line is None:
                return []
            close = ctx.closes[-1]
            if (side == OrderSide.SELL and close >= line) or (side == OrderSide.BUY and close <= line):
                return []
        intents = self._entry(ctx, side, atr_v, "funding_contrarian")
        if not intents:
            return []
        intent = intents[0]
        if p["take_profit_rr"] and intent.stop_loss is not None:
            dist = abs(ctx.price - intent.stop_loss) * to_decimal(p["take_profit_rr"])
            tp = ctx.price - dist if side == OrderSide.SELL else ctx.price + dist
            intent.take_profit = tp if tp > 0 else None
        intent.meta.update({"funding": round(recent, 8), "z": None if z is None else round(z, 3)})
        ctx.state["entered_on"] = event_time
        ctx.state["held"] = 0
        return intents

    def _manage(self, ctx: StrategyContext, sig: tuple[float, float | None, int] | None) -> list[OrderIntent]:
        held = int(ctx.state.get("held") or 0) + 1
        ctx.state["held"] = held
        p = self.params
        if p["max_hold_bars"] and held >= p["max_hold_bars"]:
            ctx.state.pop("held", None)
            return self._exit(ctx, "funding_time_exit")
        if sig is not None:
            recent = sig[0]
            short = ctx.position_qty < 0
            normalized = recent <= p["exit_rate"] if short else recent >= -p["exit_rate"]
            if normalized:
                ctx.state.pop("held", None)
                return self._exit(ctx, "funding_normalized")
        return []

    def on_stop_out(self, ctx: StrategyContext) -> None:
        super().on_stop_out(ctx)
        ctx.state.pop("held", None)


FUNDING_STRATEGIES = (FundingContrarianStrategy,)
