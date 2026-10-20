"""Kill switch handling (per account and global)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Account, AccountStatus, BotConfig, BotState, ExecutionMode, OrderSide
from app.models.system import GLOBAL_KILL_SWITCH, SystemSetting
from app.services.audit import log_event
from app.services.equity import open_positions
from app.strategy.state_machine import ACTIVE_STATES


async def close_paper_positions(db: AsyncSession, account: Account, tag: str) -> int:
    from app.services.execution import PaperExecutor

    executor = PaperExecutor()
    closed = 0
    for pos in await open_positions(db, account):
        price = Decimal(pos.mark_price or pos.entry_price)
        side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
        res = await executor.execute(
            db, account, None, pos.symbol, side, abs(pos.quantity), price,
            stop_loss=None, reduce_only=True, leverage=pos.leverage, tag=tag,
        )  # fmt: skip
        closed += int(res.ok)
    return closed


async def close_live_positions(account: Account) -> int:
    """Cancel open orders and close every position with reduce-only MARKET orders."""
    from app.services.accounts import client_for_account

    closed = 0
    async with client_for_account(account) as client:
        for p in await client.get_positions():
            qty = Decimal(str(p["positionAmt"]))
            await client.cancel_all_orders(p["symbol"])
            await client.place_order(
                symbol=p["symbol"],
                side="SELL" if qty > 0 else "BUY",
                order_type="MARKET",
                quantity=abs(qty),
                reduce_only=True,
            )
            closed += 1
    return closed


async def trigger_account_kill_switch(
    db: AsyncSession, account: Account, reason: str, *, close_positions: bool | None = None
) -> None:
    account.status = AccountStatus.KILLED
    account.status_reason = reason[:255]
    bots = await db.scalars(select(BotConfig).where(BotConfig.account_id == account.id))
    for bot in bots:
        bot.state = BotState.KILLED
    closed = 0
    should_close = settings.KILL_SWITCH_CLOSE_POSITIONS if close_positions is None else close_positions
    if should_close:
        if account.execution_mode == ExecutionMode.LIVE:
            if settings.LIVE_TRADING_ENABLED:
                closed = await close_live_positions(account)
        else:
            closed = await close_paper_positions(db, account, "kill_switch")
    await log_event(
        db,
        "risk.kill_switch",
        level="critical",
        workspace_id=account.workspace_id,
        account_id=account.id,
        reason=reason,
        positions_closed=closed,
    )


async def reset_account_kill_switch(db: AsyncSession, account: Account, user_id: uuid.UUID | None) -> None:
    """Manual reset after review: account and bots go to STOPPED and the peak is re-baselined."""
    account.status = AccountStatus.STOPPED
    account.status_reason = None
    account.peak_equity = account.last_equity
    bots = await db.scalars(select(BotConfig).where(BotConfig.account_id == account.id))
    for bot in bots:
        if bot.state == BotState.KILLED:
            bot.state = BotState.STOPPED
    await log_event(
        db, "risk.kill_switch_reset", user_id=user_id, workspace_id=account.workspace_id, account_id=account.id
    )


async def get_global_kill_switch(db: AsyncSession) -> dict:
    row = await db.get(SystemSetting, GLOBAL_KILL_SWITCH)
    return row.value if row else {"active": False}


async def set_global_kill_switch(
    db: AsyncSession, active: bool, user_id: uuid.UUID | None, reason: str | None = None
) -> dict:
    row = await db.get(SystemSetting, GLOBAL_KILL_SWITCH)
    value = {"active": active, "reason": reason, "by": str(user_id) if user_id else None}
    if row is None:
        db.add(SystemSetting(key=GLOBAL_KILL_SWITCH, value=value))
    else:
        row.value = value
    stopped = 0
    if active:
        for bot in await db.scalars(select(BotConfig).where(BotConfig.state.in_(ACTIVE_STATES))):
            bot.state = BotState.STOPPED
            stopped += 1
        for acc in await db.scalars(select(Account).where(Account.status == AccountStatus.RUNNING)):
            acc.status = AccountStatus.STOPPED
            acc.status_reason = "global_kill_switch"
    await log_event(
        db,
        "admin.global_kill_switch",
        level="critical" if active else "warning",
        user_id=user_id,
        active=active,
        reason=reason,
        bots_stopped=stopped,
    )
    return value
