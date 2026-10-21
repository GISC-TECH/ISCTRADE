"""Runs one tick of a bot: market data -> strategy -> risk -> execution, with state persistence."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Account, AccountStatus, BotConfig, BotState, ExecutionMode, OrderSide
from app.models.base import utcnow
from app.schemas.bot_options import BotOptions
from app.services.audit import log_event
from app.services.equity import compute_equity, refresh_paper_marks, roll_risk_baselines
from app.services.execution import (
    Executor,
    LiveExecutor,
    OrderService,
    PaperExecutor,
    build_executor,
    get_position,
    is_global_kill_switch_on,
)
from app.services.kill_switch import trigger_account_kill_switch
from app.services.market_data import MarketData
from app.services.news.calendar import events_around
from app.services.news.gate import AssetSentiment, GateDecision, aggregate, apply_multiplier, event_gate, news_gate
from app.services.news.store import load_signals
from app.services.position_math import stop_triggered
from app.services.risk_manager import RiskManager
from app.services.symbol_filters import SymbolFilters
from app.strategy.base import StrategyContext
from app.strategy.state_machine import BotStateMachine
from app.strategy.strategies import build_strategy

logger = logging.getLogger(__name__)


@dataclass
class TickResult:
    status: str  # "ok" | "skipped" | "killed" | "error"
    message: str = ""
    filled: int = 0
    rejected: int = 0
    reasons: list[str] = field(default_factory=list)


class BotRunner:
    def __init__(
        self,
        db: AsyncSession,
        bot: BotConfig,
        account: Account,
        market: MarketData,
        executor: Executor | None = None,
        risk_manager: RiskManager | None = None,
        filters: SymbolFilters | None = None,
    ) -> None:
        self.db = db
        self.bot = bot
        self.account = account
        self.market = market
        self.executor = executor or build_executor(account)
        self.rm = risk_manager or RiskManager()
        self.filters = filters
        self.orders = OrderService(self.executor, self.rm)
        self.now = datetime.now(UTC)
        self._event_decision = GateDecision()
        self._signals = None
        self._sentiments: dict[str, AssetSentiment] = {}

    @property
    def client(self):
        return self.executor.client if isinstance(self.executor, LiveExecutor) else None

    async def tick(self) -> TickResult:
        bot, account = self.bot, self.account
        sm = BotStateMachine(bot.state)

        if account.status == AccountStatus.KILLED:
            if bot.state != BotState.KILLED:
                bot.state = sm.kill()
            return TickResult("skipped", "account_killed")
        if await is_global_kill_switch_on(self.db):
            if bot.state not in (BotState.STOPPED, BotState.IDLE):
                bot.state = sm.stop()
            return TickResult("skipped", "global_kill_switch")
        if bot.state == BotState.STARTING:
            bot.state = sm.started()
        if bot.state != BotState.RUNNING:
            return TickResult("skipped", f"bot_{bot.state.value}")
        if account.status != AccountStatus.RUNNING:
            return TickResult("skipped", "account_not_running")

        opts = BotOptions.load(bot.options)
        runtime = dict(bot.runtime or {})
        await self._update_event_pause(opts, runtime)
        auto = opts.auto_symbols.enabled
        sym_states: dict[str, dict] = dict(runtime.get("symbols") or {}) if auto else {}
        symbols = await self._auto_symbols(opts, sym_states) if auto else [bot.symbol]
        if not symbols:
            return TickResult("skipped", "no_symbols")

        result = TickResult("ok")
        processed = 0
        skip_reason = "no_new_candle"
        for symbol in symbols:
            state = dict(sym_states.get(symbol) or {}) if auto else runtime
            new_state, skip = await self._tick_symbol(symbol, state, result, auto, opts, sym_states)
            if new_state is None:
                skip_reason = skip or skip_reason
                continue
            processed += 1
            if auto:
                sym_states[symbol] = new_state
            else:
                runtime = new_state
            if result.status == "killed":
                break
        if processed == 0:
            return TickResult("skipped", skip_reason)
        if auto:
            keep = set(symbols)
            runtime = {**runtime, "symbols": {k: v for k, v in sym_states.items() if k in keep}, "auto": symbols}
            self._save(runtime, None)
        else:
            self._save(runtime, runtime.get("last_candle"))
        return result

    async def _auto_symbols(self, opts: BotOptions, sym_states: dict[str, dict]) -> list[str]:
        """Symbols with an open position managed by this bot first, then the scanner's top-N."""
        from app.services.scanner import top_symbols

        held = []
        for symbol in sym_states:
            pos = await get_position(self.db, self.account, symbol)
            if pos is not None and pos.quantity != 0:
                held.append(symbol)
        a = opts.auto_symbols
        top = await top_symbols(
            self.db, a.top_n, min_score=a.min_score, strategy_key=self.bot.strategy_key if a.prefer_signals else None
        )
        return held + [s for s in top if s not in held]

    async def _open_positions(self, symbols: list[str]) -> int:
        n = 0
        for symbol in symbols:
            pos = await get_position(self.db, self.account, symbol)
            if pos is not None and pos.quantity != 0:
                n += 1
        return n

    async def _update_event_pause(self, opts: BotOptions, runtime: dict) -> None:
        """Evaluate the economic-event pause once per tick; audit/alert when a pause starts."""
        if not opts.event_pause.enabled:
            self._event_decision = GateDecision()
            return
        events = await events_around(self.db, self.now)
        decision, ev = event_gate(events, self.now, opts.event_pause)
        self._event_decision = decision
        marker = runtime.get("event_pause")
        if decision.block and ev is not None and marker != ev.key:
            runtime["event_pause"] = ev.key
            self.bot.runtime = {**(self.bot.runtime or {}), "event_pause": ev.key}
            until = ev.time + timedelta(minutes=opts.event_pause.minutes_after)
            await log_event(
                self.db, "bot.event_pause", workspace_id=self.bot.workspace_id, account_id=self.bot.account_id,
                bot_id=self.bot.id, bot=self.bot.name, event_id=ev.key, event_title=f"{ev.title} ({ev.country})",
                time=ev.time.strftime("%d/%m %H:%M UTC"), until=until.strftime("%d/%m %H:%M UTC"),
            )  # fmt: skip
        elif not decision.block and marker:
            runtime.pop("event_pause", None)
            self.bot.runtime = {k: v for k, v in (self.bot.runtime or {}).items() if k != "event_pause"}

    async def _sentiment(self, symbol: str, opts: BotOptions) -> AssetSentiment:
        if self._signals is None:
            window = settings.NEWS_SENTIMENT_WINDOW_HOURS
            self._signals = await load_signals(self.db, self.now - timedelta(hours=window), self.now)
        if symbol not in self._sentiments:
            self._sentiments[symbol] = aggregate(
                self._signals, symbol, self.now, settings.NEWS_SENTIMENT_HALF_LIFE_HOURS,
                settings.NEWS_SENTIMENT_WINDOW_HOURS, opts.news_filter.high_impact_minutes,
            )  # fmt: skip
        return self._sentiments[symbol]

    async def _entry_gate(
        self, symbol: str, intent, auto: bool, opts: BotOptions, sym_states: dict[str, dict]
    ) -> GateDecision:
        """Bot-level gates for NEW exposure (reduce-only exits always pass): position limit, event pause, news.

        Gates can only block or shrink an entry; they never create one. The RiskManager still validates
        every order that passes.
        """
        if intent.reduce_only:
            return GateDecision()
        if auto:
            managed = [s for s in {*sym_states, symbol} if s != symbol]
            if await self._open_positions(managed) >= opts.auto_symbols.max_positions:
                return GateDecision("max_concurrent_positions")
        decision = self._event_decision
        if decision.block:
            return decision
        if opts.news_filter.mode != "off":
            decision = decision.merge(news_gate(intent.side, await self._sentiment(symbol, opts), opts.news_filter))
        return decision

    async def _tick_symbol(
        self,
        symbol: str,
        state: dict,
        result: TickResult,
        auto: bool,
        opts: BotOptions,
        sym_states: dict[str, dict],
    ) -> tuple[dict | None, str | None]:
        bot, account = self.bot, self.account
        strategy = build_strategy(bot.strategy_key, bot.params)
        filters = self.filters if not auto else None
        if filters is None and self.client is not None:
            filters = await self.client.get_symbol_filters(symbol)
            if not auto:
                self.filters = filters

        try:
            candles = await self.market.get_closed_candles(symbol, bot.interval, max(strategy.warmup + 5, 100))
        except Exception:
            if not auto:
                raise
            logger.warning("auto bot %s: no market data for %s", bot.id, symbol)
            return None, "no_market_data"
        if not candles:
            return None, "no_market_data"
        last = candles[-1]
        last_seen = state.get("last_candle")
        if last_seen is not None and last.open_time <= int(last_seen):
            return None, "no_new_candle"
        new_candles = [c for c in candles if last_seen is None or c.open_time > int(last_seen)]
        price = Decimal(str(last.close))

        if self.client is not None:
            from app.services.reconciler import Reconciler

            await Reconciler(self.db, account, self.client).sync_positions([symbol])

        # Paper mode: simulate protective stops on the candles since the last tick.
        pos = await get_position(self.db, account, symbol)
        if isinstance(self.executor, PaperExecutor) and pos is not None and pos.quantity != 0:
            for c in new_candles:
                if stop_triggered(pos.quantity, pos.stop_loss, Decimal(str(c.low)), Decimal(str(c.high))):
                    stop_price = Decimal(pos.stop_loss)
                    side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
                    await self.executor.execute(
                        self.db, account, bot, symbol, side, abs(pos.quantity), stop_price,
                        stop_loss=None, reduce_only=True, leverage=bot.leverage, tag="stop_loss",
                        fill_price=stop_price,
                    )  # fmt: skip
                    await log_event(
                        self.db, "position.stop_loss", level="warning", workspace_id=account.workspace_id,
                        account_id=account.id, symbol=symbol, price=stop_price,
                    )  # fmt: skip
                    ctx_stop = self._context(candles, Decimal(0), Decimal(0), Decimal(0), state, symbol)
                    strategy.on_stop_out(ctx_stop)
                    break

        if account.execution_mode != ExecutionMode.LIVE:
            await refresh_paper_marks(self.db, account, {symbol: price})
        equity = (await compute_equity(self.db, account, self.client)).equity
        roll_risk_baselines(account, equity)
        risk_state = self.rm.from_account(account, equity)
        if self.rm.is_kill_switch_triggered(risk_state):
            await trigger_account_kill_switch(self.db, account, f"drawdown {self.rm.drawdown_pct(risk_state):.2f}%")
            result.status, result.message = "killed", "drawdown_kill_switch"
            return {**state, "last_candle": last.open_time}, None

        pos = await get_position(self.db, account, symbol)
        ctx = self._context(
            candles, pos.quantity if pos else Decimal(0), pos.entry_price if pos else Decimal(0), equity, state, symbol
        )
        for intent in strategy.on_candle(ctx):
            gate = await self._entry_gate(symbol, intent, auto, opts, sym_states)
            if gate.block:
                strategy.on_result(intent, False, ctx)
                result.rejected += 1
                result.reasons.append(gate.block)
                if gate.block != "max_concurrent_positions":
                    await log_event(
                        self.db, "bot.entry_filtered", workspace_id=account.workspace_id, account_id=account.id,
                        bot_id=bot.id, symbol=symbol, side=intent.side, reason=gate.block,
                    )  # fmt: skip
                continue
            intent = apply_multiplier(intent, gate.multiplier)
            res = await self.orders.submit(
                self.db, account, intent, symbol=symbol, ref_price=price, leverage=bot.leverage,
                risk_state=risk_state, bot=bot, filters=filters,
                take_profit=None if intent.reduce_only else intent.take_profit,
            )  # fmt: skip
            strategy.on_result(intent, res.ok, ctx)
            if res.ok:
                result.filled += 1
            else:
                result.rejected += 1
                result.reasons.append(res.reason)
            if res.kill_switch:
                await trigger_account_kill_switch(self.db, account, res.reason)
                result.status, result.message = "killed", res.reason
                break
            pos = await get_position(self.db, account, symbol)
            ctx.position_qty = pos.quantity if pos else Decimal(0)
            ctx.position_entry = pos.entry_price if pos else Decimal(0)
            equity = (await compute_equity(self.db, account, self.client)).equity
            ctx.equity = equity
            risk_state = self.rm.from_account(account, equity)

        return {**ctx.state, "last_candle": last.open_time}, None

    def _context(self, candles, qty, entry, equity, state, symbol: str | None = None) -> StrategyContext:
        return StrategyContext(
            symbol=symbol or self.bot.symbol,
            candles=candles,
            position_qty=Decimal(qty),
            position_entry=Decimal(entry),
            equity=Decimal(equity),
            leverage=self.bot.leverage,
            params=self.bot.params,
            state=state,
        )

    def _save(self, state: dict, last_candle: int | None) -> None:
        new_state = dict(state)
        if last_candle is not None:
            new_state["last_candle"] = last_candle
        self.bot.runtime = new_state  # reassign -> SQLAlchemy detects the JSON change
        self.bot.last_tick_at = utcnow()
        self.bot.last_error = None


async def run_bot_tick(db: AsyncSession, bot: BotConfig, account: Account, market: MarketData, **kwargs) -> TickResult:
    """Run a tick and commit; on failure the bot goes to ERROR and the error is audited."""
    try:
        result = await BotRunner(db, bot, account, market, **kwargs).tick()
        await db.commit()
        return result
    except Exception as exc:
        logger.exception("bot tick failed bot=%s", bot.id)
        await db.rollback()
        await db.refresh(bot)
        bot.state = BotState.ERROR
        bot.last_error = str(exc)[:2000]
        await log_event(
            db, "bot.error", level="error", workspace_id=bot.workspace_id, account_id=bot.account_id,
            bot_id=bot.id, error=str(exc)[:500],
        )  # fmt: skip
        await db.commit()
        return TickResult("error", str(exc))
