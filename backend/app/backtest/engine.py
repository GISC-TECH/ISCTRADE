"""Event-driven backtest engine.

Runs the SAME strategies and RiskManager used by live/paper bots over historical candles.

Execution model (documented assumptions):
- Signals are evaluated on candle close; market orders fill at the close +/- slippage.
- Protective stops are checked against each candle's high/low BEFORE the strategy runs and fill at the
  stop price +/- slippage (gaps beyond the stop fill at the candle open if worse).
- Optional take profits (``OrderIntent.take_profit``) are checked after the stop (if a candle touches both, the
  stop wins: pessimistic) and fill at the take-profit price -/+ slippage (no favourable gap fills).
- Fees are charged on notional for every fill (taker rate by default).
- Optional liquidity cap (``max_volume_pct`` > 0): an entry fills at most that % of the signal candle volume; the
  rest expires (counted in ``partial_fills``). Exits are never capped.
- Funding (optional, ``funding=[FundingEvent]``): at each funding timestamp inside a candle the position held at
  the candle open pays/receives ``qty * price * rate`` (long pays a positive rate), with ``price`` interpolated
  between the candle open and close. See app/backtest/funding.py for the data sources.
- One-way net position; leverage only affects the margin check.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.backtest.data import interval_ms
from app.backtest.funding import FundingEvent
from app.models.enums import OrderSide
from app.services.news.gate import GateDecision, apply_multiplier
from app.services.position_math import apply_fill, stop_triggered
from app.services.risk_manager import AccountRiskState, OrderRequest, RiskManager
from app.services.symbol_filters import SymbolFilters, default_filters
from app.strategy.base import Candle, OrderIntent, Strategy, StrategyContext

ZERO = Decimal("0")
YEAR_MS = 365 * 86_400_000


@dataclass
class BacktestConfig:
    initial_balance: Decimal = Decimal("10000")
    fee_rate: Decimal = Decimal("0.0004")
    slippage_bps: Decimal = Decimal("2")
    leverage: int = 1
    max_risk_per_trade_pct: Decimal = Decimal("1")
    max_leverage: int = 5
    max_daily_loss_pct: Decimal = Decimal("3")
    max_drawdown_pct: Decimal = Decimal("15")
    stop_on_kill_switch: bool = True
    # 0 = off; else each entry fills at most this % of the signal candle volume (the rest expires, like paper)
    max_volume_pct: Decimal = Decimal("0")
    lookback: int | None = None  # candles passed to the strategy each step (default: warmup*3, min 200)
    filters: SymbolFilters | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> BacktestConfig:
        data = dict(data or {})
        kwargs: dict[str, Any] = {}
        for name, f in cls.__dataclass_fields__.items():
            if name == "filters" or data.get(name) is None:
                continue
            default, value = f.default, data[name]
            if isinstance(default, Decimal):
                kwargs[name] = Decimal(str(value))
            elif isinstance(default, bool):
                kwargs[name] = bool(value)
            else:
                kwargs[name] = int(value)
        return cls(**kwargs)

    def public_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("filters", None)
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}


@dataclass
class BacktestTrade:
    time: int
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    realized_pnl: Decimal
    tag: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "time": self.time,
            "side": self.side,
            "quantity": str(self.quantity),
            "price": str(self.price),
            "fee": str(self.fee),
            "realized_pnl": str(self.realized_pnl),
            "tag": self.tag,
        }


@dataclass
class BacktestResult:
    metrics: dict[str, Any]
    equity_curve: list[dict[str, Any]]
    trades: list[BacktestTrade]
    rejections: dict[str, int] = field(default_factory=dict)
    killed_at: int | None = None

    def as_dict(self, max_points: int = 1000) -> dict[str, Any]:
        return {
            "metrics": self.metrics,
            "equity_curve": downsample(self.equity_curve, max_points),
            "trades": [t.as_dict() for t in self.trades],
            "rejections": self.rejections,
            "killed_at": self.killed_at,
        }


def downsample(points: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    if len(points) <= max_points:
        return points
    step = math.ceil(len(points) / max_points)
    out = points[::step]
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


EntryGate = Callable[[OrderIntent, Candle], GateDecision]


class BacktestEngine:
    def __init__(
        self,
        strategy: Strategy,
        symbol: str,
        interval: str,
        config: BacktestConfig | None = None,
        entry_gate: EntryGate | None = None,
        funding: list[FundingEvent] | None = None,
    ) -> None:
        self.strategy = strategy
        self.funding = sorted(funding or [], key=lambda e: e.time)
        self._funding_times = [e.time for e in self.funding]
        # strategies that read funding see the settled events up to the current candle (no look-ahead)
        self._funding_ctx = max(getattr(strategy, "funding_lookback", 0), 0) if strategy.needs_funding else 0
        self.funding_total = ZERO  # net funding received (negative = paid)
        self.funding_applied = 0
        self.entry_gate = entry_gate  # optional news/event filter (same logic as the live bots)
        self.symbol = symbol
        self.interval = interval
        self.cfg = config or BacktestConfig()
        self.filters = self.cfg.filters or default_filters(symbol)
        self.rm = RiskManager()
        self.slip = self.cfg.slippage_bps / Decimal(10000)
        # mutable simulation state
        self.balance = self.cfg.initial_balance
        self.qty = ZERO
        self.entry = ZERO
        self.stop: Decimal | None = None
        self.tp: Decimal | None = None
        self.peak = self.cfg.initial_balance
        self.day: int | None = None
        self.day_start_equity = self.cfg.initial_balance
        self.trades: list[BacktestTrade] = []
        self.rejections: dict[str, int] = {}
        self.fees = ZERO
        self.partial_fills = 0

    # ------------------------------------------------------------------ #
    def equity(self, price: Decimal) -> Decimal:
        return self.balance + self.qty * (price - self.entry) if self.qty else self.balance

    def risk_state(self, equity: Decimal) -> AccountRiskState:
        return AccountRiskState(
            equity=equity,
            peak_equity=max(self.peak, equity),
            day_start_equity=self.day_start_equity,
            max_risk_per_trade_pct=self.cfg.max_risk_per_trade_pct,
            max_leverage=self.cfg.max_leverage,
            max_daily_loss_pct=self.cfg.max_daily_loss_pct,
            max_drawdown_pct=self.cfg.max_drawdown_pct,
        )

    def _fill(self, time: int, side: OrderSide, qty: Decimal, price: Decimal, tag: str) -> Decimal:
        outcome = apply_fill(self.qty, self.entry, side, qty, price)
        fee = qty * price * self.cfg.fee_rate
        self.balance += outcome.realized_pnl - fee
        self.fees += fee
        self.qty, self.entry = outcome.quantity, outcome.entry_price
        if self.qty == 0:
            self.stop = None
            self.tp = None
        self.trades.append(BacktestTrade(time, side.value, qty, price, fee, outcome.realized_pnl, tag))
        return outcome.realized_pnl

    def _reject(self, reason: str) -> None:
        key = reason.split(":")[0]
        self.rejections[key] = self.rejections.get(key, 0) + 1

    def _close_all(self, time: int, price: Decimal, tag: str) -> None:
        if self.qty:
            side = OrderSide.SELL if self.qty > 0 else OrderSide.BUY
            px = price * (1 - self.slip) if side == OrderSide.SELL else price * (1 + self.slip)
            self._fill(time, side, abs(self.qty), px, tag)

    def _execute_intent(self, intent: OrderIntent, candle: Candle, equity: Decimal) -> bool:
        price = Decimal(str(candle.close))
        state = self.risk_state(equity)
        qty = intent.quantity
        if intent.reduce_only:
            opposite = (self.qty > 0 and intent.side == OrderSide.SELL) or (
                self.qty < 0 and intent.side == OrderSide.BUY
            )
            if not opposite:
                self._reject("reduce_only_no_position")
                return False
            qty = min(qty or abs(self.qty), abs(self.qty))
        elif qty is None:
            if intent.stop_loss is None:
                self._reject("stop_loss_required")
                return False
            qty = self.rm.max_position_size(price, intent.stop_loss, state) * intent.risk_fraction
        if not intent.reduce_only and self.cfg.max_volume_pct > 0:
            cap = Decimal(str(candle.volume)) * self.cfg.max_volume_pct / Decimal(100)
            if abs(qty) > cap:
                qty = cap
                self.partial_fills += 1
        qty = self.filters.round_qty(abs(qty))
        if qty <= 0:
            self._reject("size_zero_after_rounding")
            return False
        problem = self.filters.check(qty, price, intent.reduce_only)
        if problem:
            self._reject(problem)
            return False
        decision = self.rm.evaluate(
            OrderRequest(self.symbol, intent.side, qty, price, intent.stop_loss, self.cfg.leverage, intent.reduce_only),
            state,
        )
        if not decision.allowed:
            self._reject(decision.reason)
            return False
        px = price * (1 + self.slip) if intent.side == OrderSide.BUY else price * (1 - self.slip)
        self._fill(candle.close_time, intent.side, qty, px, intent.tag)
        if not intent.reduce_only and self.qty != 0:
            if intent.stop_loss is not None:
                self.stop = intent.stop_loss
            tp = intent.take_profit
            valid_tp = tp is not None and tp > 0 and ((tp > px) if self.qty > 0 else (tp < px))
            self.tp = tp if valid_tp else None
        return True

    # ------------------------------------------------------------------ #
    def _apply_funding(self, candle: Candle, cursor: int) -> int:
        """Settle every funding event inside ``candle`` on the position held at its open. Returns the new cursor."""
        events = self.funding
        while cursor < len(events) and events[cursor].time < candle.open_time:
            cursor += 1  # before the simulated period
        while cursor < len(events) and events[cursor].time <= candle.close_time:
            ev = events[cursor]
            cursor += 1
            if not self.qty:
                continue
            span = max(candle.close_time - candle.open_time, 1)
            frac = Decimal(ev.time - candle.open_time) / Decimal(span)
            o, c = Decimal(str(candle.open)), Decimal(str(candle.close))
            price = o + (c - o) * frac
            payment = -self.qty * price * ev.rate
            self.balance += payment
            self.funding_total += payment
            self.funding_applied += 1
        return cursor

    def run(self, candles: list[Candle], start_index: int = 0) -> BacktestResult:
        """Simulate ``candles[start_index:]``; earlier candles are history only (indicator warm-up)."""
        if not candles or start_index >= len(candles):
            raise ValueError("no candles")
        lookback = self.cfg.lookback or max(self.strategy.warmup * 3, 200)
        state: dict[str, Any] = {}
        curve: list[dict[str, Any]] = []
        killed_at: int | None = None
        fcur = 0

        for i in range(start_index, len(candles)):
            candle = candles[i]
            close = Decimal(str(candle.close))
            # 0. funding settlements during this candle (position held since the previous close)
            if self.funding:
                fcur = self._apply_funding(candle, fcur)
            # 1. protective stop
            if stop_triggered(self.qty, self.stop, Decimal(str(candle.low)), Decimal(str(candle.high))):
                stop = Decimal(self.stop)  # type: ignore[arg-type]
                open_ = Decimal(str(candle.open))
                ref = min(stop, open_) if self.qty > 0 else max(stop, open_)  # gap through the stop
                side = OrderSide.SELL if self.qty > 0 else OrderSide.BUY
                px = ref * (1 - self.slip) if side == OrderSide.SELL else ref * (1 + self.slip)
                self._fill(candle.close_time, side, abs(self.qty), px, "stop_loss")
                self.strategy.on_stop_out(self._ctx(candles, i, lookback, close, state))
            # 1b. take profit (after the stop: a candle touching both counts as a stop)
            elif self.qty and self.tp is not None:
                long = self.qty > 0
                hi, lo = Decimal(str(candle.high)), Decimal(str(candle.low))
                if (long and hi >= self.tp) or (not long and lo <= self.tp):
                    tp = Decimal(self.tp)
                    side = OrderSide.SELL if long else OrderSide.BUY
                    px = tp * (1 - self.slip) if long else tp * (1 + self.slip)
                    self._fill(candle.close_time, side, abs(self.qty), px, "take_profit")
                    self.strategy.on_stop_out(self._ctx(candles, i, lookback, close, state))

            # 2. risk baselines
            equity = self.equity(close)
            day = candle.open_time // 86_400_000
            if day != self.day:
                self.day = day
                self.day_start_equity = equity
            self.peak = max(self.peak, equity)

            # 3. drawdown kill switch
            if self.rm.is_kill_switch_triggered(self.risk_state(equity)):
                self._close_all(candle.close_time, close, "kill_switch")
                killed_at = candle.close_time
                curve.append({"time": candle.close_time, "equity": float(self.equity(close))})
                if self.cfg.stop_on_kill_switch:
                    break

            # 4. strategy
            ctx = self._ctx(candles, i, lookback, close, state)
            for intent in self.strategy.on_candle(ctx):
                if self.entry_gate is not None and not intent.reduce_only:
                    decision = self.entry_gate(intent, candle)
                    if decision.block:
                        self._reject(decision.block.split(":", 1)[0])
                        self.strategy.on_result(intent, False, ctx)
                        continue
                    intent = apply_multiplier(intent, decision.multiplier)
                ok = self._execute_intent(intent, candle, self.equity(close))
                self.strategy.on_result(intent, ok, ctx)
                ctx.position_qty, ctx.position_entry = self.qty, self.entry
                ctx.equity = self.equity(close)

            curve.append({"time": candle.close_time, "equity": float(self.equity(close))})

        metrics = compute_metrics(
            curve, self.trades, self.cfg.initial_balance, self.fees, interval_ms(self.interval), candles[start_index:]
        )
        metrics["total_funding"] = round(float(self.funding_total), 4)
        metrics["funding_events_applied"] = self.funding_applied
        metrics["partial_fills"] = self.partial_fills
        return BacktestResult(metrics, curve, self.trades, self.rejections, killed_at)

    def _ctx(self, candles: list[Candle], i: int, lookback: int, price: Decimal, state: dict) -> StrategyContext:
        funding: list[FundingEvent] = []
        if self._funding_ctx and self.funding:
            end = bisect.bisect_right(self._funding_times, candles[i].close_time)
            funding = self.funding[max(0, end - self._funding_ctx) : end]
        return StrategyContext(
            symbol=self.symbol,
            candles=candles[max(0, i + 1 - lookback) : i + 1],
            position_qty=self.qty,
            position_entry=self.entry,
            equity=self.equity(price),
            leverage=self.cfg.leverage,
            params=self.strategy.params,
            state=state,
            funding=funding,
        )


def max_drawdown_pct(values: list[float]) -> float:
    peak = -math.inf
    mdd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak * 100)
    return mdd


def sharpe_ratio(values: list[float], bar_ms: int) -> float:
    if len(values) < 3:
        return 0.0
    rets = [(b - a) / a for a, b in zip(values, values[1:]) if a > 0]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return mean / sd * math.sqrt(YEAR_MS / bar_ms)


def compute_metrics(
    curve: list[dict[str, Any]],
    trades: list[BacktestTrade],
    initial: Decimal,
    fees: Decimal,
    bar_ms: int,
    candles: list[Candle],
) -> dict[str, Any]:
    values = [p["equity"] for p in curve] or [float(initial)]
    final = values[-1]
    closing = [t for t in trades if t.realized_pnl != 0]
    wins = [t for t in closing if t.realized_pnl > 0]
    losses = [t for t in closing if t.realized_pnl < 0]
    gross_win = sum((t.realized_pnl for t in wins), ZERO)
    gross_loss = -sum((t.realized_pnl for t in losses), ZERO)
    first, last = candles[0].close, candles[-1].close
    return {
        "initial_balance": float(initial),
        "final_equity": round(final, 2),
        "net_pnl": round(final - float(initial), 2),
        "return_pct": round((final / float(initial) - 1) * 100, 4),
        "max_drawdown_pct": round(max_drawdown_pct(values), 4),
        "sharpe": round(sharpe_ratio(values, bar_ms), 4),
        "num_fills": len(trades),
        "num_closed_trades": len(closing),
        "win_rate_pct": round(len(wins) / len(closing) * 100, 2) if closing else 0.0,
        "profit_factor": round(float(gross_win / gross_loss), 4) if gross_loss > 0 else None,
        "avg_win": round(float(gross_win / len(wins)), 4) if wins else 0.0,
        "avg_loss": round(float(-gross_loss / len(losses)), 4) if losses else 0.0,
        "total_fees": round(float(fees), 4),
        "buy_hold_return_pct": round((last / first - 1) * 100, 4) if first else 0.0,
        "candles": len(candles),
        "start": datetime.fromtimestamp(candles[0].open_time / 1000, tz=timezone.utc).isoformat(),
        "end": datetime.fromtimestamp(candles[-1].close_time / 1000, tz=timezone.utc).isoformat(),
    }


def run_backtest(
    strategy: Strategy,
    candles: list[Candle],
    symbol: str,
    interval: str,
    config: BacktestConfig | None = None,
    entry_gate: EntryGate | None = None,
    funding: list[FundingEvent] | None = None,
    start_index: int = 0,
) -> BacktestResult:
    return BacktestEngine(strategy, symbol, interval, config, entry_gate, funding).run(candles, start_index)
