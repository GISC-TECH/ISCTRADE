"""Reconciler: keeps local positions/orders consistent with Binance (live accounts)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account
from app.services.binance_client import BinanceFuturesClient


class Reconciler:
    def __init__(self, db: AsyncSession, account: Account, client: BinanceFuturesClient) -> None:
        self.db = db
        self.account = account
        self.client = client

    async def sync_positions(self, symbols: list[str] | None = None) -> int:
        from app.services.execution import get_or_create_position

        changed = 0
        remote = {p["symbol"]: p for p in await self.client.get_positions(include_flat=True)}
        for symbol in symbols or list(remote):
            p = remote.get(symbol)
            pos = await get_or_create_position(self.db, self.account, symbol)
            qty = Decimal(str(p["positionAmt"])) if p else Decimal(0)
            if pos.quantity != qty:
                changed += 1
            pos.quantity = qty
            pos.entry_price = Decimal(str(p.get("entryPrice", "0"))) if p else Decimal(0)
            pos.mark_price = Decimal(str(p.get("markPrice", "0"))) if p else None
            pos.unrealized_pnl = Decimal(str(p.get("unRealizedProfit", "0"))) if p else Decimal(0)
            pos.is_paper = False
        return changed
