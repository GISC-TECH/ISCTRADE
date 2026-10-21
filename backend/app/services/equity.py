"""Equity tracking: current equity, daily/peak risk baselines and periodic snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exchanges.base import ExchangeAdapter
from app.models import Account, EquitySnapshot, ExecutionMode, Position
from app.models.base import as_utc, utcnow


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


def roll_risk_baselines(account: Account, equity: Decimal, now: datetime | None = None) -> None:
    """Update peak equity and reset the daily baseline at 00:00 UTC."""
    now = now or utcnow()
    day_start = as_utc(account.day_start_at)
    if account.day_start_equity is None or day_start is None or day_start.date() != now.date():
        account.day_start_equity = equity
        account.day_start_at = now
    if account.peak_equity is None or equity > Decimal(account.peak_equity):
        account.peak_equity = equity
    account.last_equity = equity
    account.last_synced_at = now


async def take_snapshot(db: AsyncSession, account: Account, client: ExchangeAdapter | None = None) -> EquitySnapshot:
    state = await compute_equity(db, account, client)
    roll_risk_baselines(account, state.equity)
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
