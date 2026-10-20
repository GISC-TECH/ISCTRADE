"""Equity tracking (placeholder): periodic snapshots used by charts, daily loss and drawdown rules."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass
class EquitySnapshot:
    account_id: str
    timestamp: datetime
    wallet_balance: Decimal
    unrealized_pnl: Decimal

    @property
    def equity(self) -> Decimal:
        return self.wallet_balance + self.unrealized_pnl


def peak_equity(snapshots: list[EquitySnapshot]) -> Decimal:
    return max((s.equity for s in snapshots), default=Decimal(0))
