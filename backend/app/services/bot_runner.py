"""Runs one tick of a bot: market data -> strategy -> risk -> execution, with state persistence."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, AccountStatus, BotConfig, BotState, ExecutionMode, OrderSide
from app.models.base import utcnow
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

        strategy = build_strategy(bot.strategy_key, bot.params)
        if self.filters is None and self.client is not None:
            self.filters = await self.client.get_symbol_filters(bot.symbol)

        candles = await self.market.get_closed_candles(bot.symbol, bot.interval, max(strategy.warmup + 5, 100))
        if not candles:
            return TickResult("skipped", "no_market_data")
        state = dict(bot.runtime or {})
        last = candles[-1]
        last_seen = state.get("last_candle")
        if last_seen is not None and last.open_time <= int(last_seen):
            return TickResult("skipped", "no_new_candle")
        new_candles = [c for c in candles if last_seen is None or c.open_time > int(last_seen)]
        price = Decimal(str(last.close))
        result = TickResult("ok")

        if self.client is not None:
            from app.services.reconciler import Reconciler

            await Reconciler(self.db, account, self.client).sync_positions([bot.symbol])

        # Paper mode: simulate protective stops on the candles since the last tick.
        pos = await get_position(self.db, account, bot.symbol)
        if isinstance(self.executor, PaperExecutor) and pos is not None and pos.quantity != 0:
            for c in new_candles:
                if stop_triggered(pos.quantity, pos.stop_loss, Decimal(str(c.low)), Decimal(str(c.high))):
                    stop_price = Decimal(pos.stop_loss)
                    side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
                    await self.executor.execute(
                        self.db, account, bot, bot.symbol, side, abs(pos.quantity), stop_price,
                        stop_loss=None, reduce_only=True, leverage=bot.leverage, tag="stop_loss",
                        fill_price=stop_price,
                    )  # fmt: skip
                    await log_event(
                        self.db, "position.stop_loss", level="warning", workspace_id=account.workspace_id,
                        account_id=account.id, symbol=bot.symbol, price=stop_price,
                    )  # fmt: skip
                    ctx_stop = self._context(candles, Decimal(0), Decimal(0), Decimal(0), state)
                    strategy.on_stop_out(ctx_stop)
                    break

        if account.execution_mode != ExecutionMode.LIVE:
            await refresh_paper_marks(self.db, account, {bot.symbol: price})
        equity = (await compute_equity(self.db, account, self.client)).equity
        roll_risk_baselines(account, equity)
        risk_state = self.rm.from_account(account, equity)
        if self.rm.is_kill_switch_triggered(risk_state):
            await trigger_account_kill_switch(self.db, account, f"drawdown {self.rm.drawdown_pct(risk_state):.2f}%")
            self._save(state, last.open_time)
            return TickResult("killed", "drawdown_kill_switch")

        pos = await get_position(self.db, account, bot.symbol)
        ctx = self._context(
            candles, pos.quantity if pos else Decimal(0), pos.entry_price if pos else Decimal(0), equity, state
        )
        for intent in strategy.on_candle(ctx):
            res = await self.orders.submit(
                self.db, account, intent, symbol=bot.symbol, ref_price=price, leverage=bot.leverage,
                risk_state=risk_state, bot=bot, filters=self.filters,
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
            pos = await get_position(self.db, account, bot.symbol)
            ctx.position_qty = pos.quantity if pos else Decimal(0)
            ctx.position_entry = pos.entry_price if pos else Decimal(0)
            equity = (await compute_equity(self.db, account, self.client)).equity
            ctx.equity = equity
            risk_state = self.rm.from_account(account, equity)

        self._save(ctx.state, last.open_time)
        return result

    def _context(self, candles, qty, entry, equity, state) -> StrategyContext:
        return StrategyContext(
            symbol=self.bot.symbol,
            candles=candles,
            position_qty=Decimal(qty),
            position_entry=Decimal(entry),
            equity=Decimal(equity),
            leverage=self.bot.leverage,
            params=self.bot.params,
            state=state,
        )

    def _save(self, state: dict, last_candle: int) -> None:
        new_state = dict(state)
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
