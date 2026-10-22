"""Equity tracking: current equity, daily/peak risk baselines and periodic snapshots."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.exchanges.base import ExchangeAdapter, UnsupportedFeatureError
from app.models import Account, EquitySnapshot, ExecutionMode, Position
from app.models.base import as_utc, utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EquityState:
    wallet_balance: Decimal
    unrealized_pnl: Decimal
    available_balance: Decimal

    @property
    def equity(self) -> Decimal:
        return self.wallet_balance + self.unrealized_pnl


async def open_positions(db: AsyncSession, account: Account) -> list[Position]:
    rows = await db.scalars(select(Position).where(Position.account_id == account.id, Position.quantity != 0))
    return list(rows)


async def refresh_paper_marks(db: AsyncSession, account: Account, prices: dict[str, Decimal]) -> None:
    for pos in await open_positions(db, account):
        price = prices.get(pos.symbol)
        if price is not None:
            pos.mark_price = price
            pos.unrealized_pnl = pos.quantity * (price - pos.entry_price)


async def paper_equity(db: AsyncSession, account: Account) -> EquityState:
    positions = await open_positions(db, account)
    unrealized = sum((Decimal(p.unrealized_pnl) for p in positions), Decimal("0"))
    margin = sum(
        (abs(p.quantity) * Decimal(p.entry_price) / Decimal(max(p.leverage, 1)) for p in positions), Decimal("0")
    )
    wallet = Decimal(account.paper_balance)
    return EquityState(wallet, unrealized, wallet + unrealized - margin)


async def live_equity(client: ExchangeAdapter) -> EquityState:
    data = await client.get_account()
    return EquityState(
        wallet_balance=Decimal(str(data.get("totalWalletBalance", "0"))),
        unrealized_pnl=Decimal(str(data.get("totalUnrealizedProfit", "0"))),
        available_balance=Decimal(str(data.get("availableBalance", "0"))),
    )


async def compute_equity(db: AsyncSession, account: Account, client: ExchangeAdapter | None = None) -> EquityState:
    if account.execution_mode == ExecutionMode.LIVE and client is not None:
        return await live_equity(client)
    return await paper_equity(db, account)


def roll_risk_baselines(
    account: Account, equity: Decimal, now: datetime | None = None, *, update_peak: bool = True
) -> None:
    """Update peak equity and reset the daily baseline at 00:00 UTC.

    `update_peak=False` when the transfer history could not be read: a new high might be a deposit that was
    not folded into the baselines yet (it would mask later losses).
    """
    now = now or utcnow()
    day_start = as_utc(account.day_start_at)
    if account.day_start_equity is None or day_start is None or day_start.date() != now.date():
        account.day_start_equity = equity
        account.day_start_at = now
    if account.peak_equity is None or (update_peak and equity > Decimal(account.peak_equity)):
        account.peak_equity = equity
    account.last_equity = equity
    account.last_synced_at = now


def _thresholds_hit(account: Account, equity: Decimal) -> bool:
    """New high or a drawdown/daily-loss limit reached: verify transfers before acting on it."""
    peak = Decimal(account.peak_equity) if account.peak_equity is not None else None
    day = Decimal(account.day_start_equity) if account.day_start_equity is not None else None
    if peak is not None and equity > peak:
        return True
    if peak and (peak - equity) / peak * 100 >= Decimal(account.max_drawdown_pct):
        return True
    return bool(day and (day - equity) / day * 100 >= Decimal(account.max_daily_loss_pct))


async def sync_transfers(
    account: Account, client: ExchangeAdapter, now: datetime | None = None, db: AsyncSession | None = None
) -> bool:
    """Fold deposits/withdrawals since the cursor into `peak_equity` and `day_start_equity` (G-03).

    A withdrawal lowers both baselines (it is not a loss: no kill switch); a deposit raises them (it cannot
    mask a loss). Only transfers after the start of the day move the daily baseline (the day-start equity
    measured after an earlier transfer already includes it). Returns False when the history is unavailable
    (the cursor stays, so nothing is lost; the caller must not raise the peak meanwhile).
    """
    now = now or utcnow()
    end = now - timedelta(seconds=settings.TRANSFER_SETTLE_SECONDS)
    cursor = as_utc(account.transfers_synced_at)
    if cursor is None:  # first sync: the baselines are measured from now on
        account.transfers_synced_at = end
        return True
    if end <= cursor:
        return True
    try:
        rows = await client.get_transfers(int(cursor.timestamp() * 1000) + 1, int(end.timestamp() * 1000))
    except UnsupportedFeatureError:
        account.transfers_synced_at = end
        return True
    except Exception as exc:
        logger.warning("transfer history unavailable for account %s: %s", account.id, exc)
        return False
    day_start = as_utc(account.day_start_at)
    day_start_ms = int(day_start.timestamp() * 1000) if day_start else None
    net = net_today = Decimal("0")
    for row in rows:
        if str(row.get("asset") or "USDT").upper() not in settings.TRANSFER_ASSETS:
            continue
        amount = Decimal(str(row.get("amount") or "0"))
        net += amount
        if day_start_ms is not None and int(row.get("time") or 0) >= day_start_ms:
            net_today += amount
    if net and account.peak_equity is not None:
        account.peak_equity = max(Decimal(account.peak_equity) + net, Decimal("0"))
    if net_today and account.day_start_equity is not None:
        account.day_start_equity = max(Decimal(account.day_start_equity) + net_today, Decimal("0"))
    account.transfers_synced_at = end
    if net:
        logger.info("account %s: transfers %s USDT folded into risk baselines", account.id, net)
        if db is not None:
            from app.services.audit import log_event

            await log_event(
                db, "risk.transfers_applied", workspace_id=account.workspace_id, account_id=account.id,
                net=net, net_today=net_today, transfers=len(rows), peak_equity=account.peak_equity,
                day_start_equity=account.day_start_equity,
            )  # fmt: skip
    return True


async def refresh_risk_baselines(
    account: Account,
    equity: Decimal,
    client: ExchangeAdapter | None = None,
    now: datetime | None = None,
    db: AsyncSession | None = None,
) -> None:
    """`roll_risk_baselines`, after folding wallet transfers into the baselines of live accounts.

    The transfer history is read at most every TRANSFER_SYNC_SECONDS, and always before a new high or a
    drawdown/daily-loss limit is acted upon (so a withdrawal never trips the kill switch).
    """
    now = now or utcnow()
    synced = True
    if account.execution_mode == ExecutionMode.LIVE and client is not None:
        cursor = as_utc(account.transfers_synced_at)
        due = cursor is None or (now - cursor).total_seconds() >= settings.TRANSFER_SYNC_SECONDS
        if due or _thresholds_hit(account, equity):
            synced = await sync_transfers(account, client, now, db)
    roll_risk_baselines(account, equity, now, update_peak=synced)


async def take_snapshot(db: AsyncSession, account: Account, client: ExchangeAdapter | None = None) -> EquitySnapshot:
    state = await compute_equity(db, account, client)
    await refresh_risk_baselines(account, state.equity, client, db=db)
    snap = EquitySnapshot(
        workspace_id=account.workspace_id,
        account_id=account.id,
        wallet_balance=state.wallet_balance,
        unrealized_pnl=state.unrealized_pnl,
        equity=state.equity,
        is_paper=account.execution_mode != ExecutionMode.LIVE,
        taken_at=utcnow(),
    )
    db.add(snap)
    return snap


def peak_equity(values: list[Decimal]) -> Decimal:
    return max(values, default=Decimal(0))
