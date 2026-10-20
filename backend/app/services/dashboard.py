"""Account dashboard data: paper ledger or real exchange data."""

from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, ExecutionMode, Trade
from app.services.binance_client import BinanceAPIError, BinanceFuturesClient
from app.services.equity import open_positions, paper_equity

logger = logging.getLogger(__name__)


def _day_start() -> datetime:
    return datetime.combine(datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc)


def _pct(part: Decimal, whole: Decimal | None) -> float:
    return float(part / whole * 100) if whole else 0.0


async def build_dashboard(
    db: AsyncSession, account: Account, client: BinanceFuturesClient | None = None
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "account_id": account.id,
        "status": account.status,
        "status_reason": account.status_reason,
        "execution_mode": account.execution_mode,
        "is_testnet": account.is_testnet,
        "peak_equity": account.peak_equity,
        "day_start_equity": account.day_start_equity,
        "error": None,
    }
    if account.execution_mode == ExecutionMode.LIVE and client is not None:
        try:
            return {**base, **(await _exchange_data(client))}
        except (BinanceAPIError, OSError, Exception) as exc:  # show last known paper/db data + error
            logger.warning("dashboard exchange fetch failed: %s", exc)
            base["error"] = f"Falha ao consultar a Binance: {exc}"[:300]
    return {**base, **(await _paper_data(db, account))}


async def _paper_data(db: AsyncSession, account: Account) -> dict[str, Any]:
    eq = await paper_equity(db, account)
    realized_today = await db.scalar(
        select(func.coalesce(func.sum(Trade.realized_pnl - Trade.fee), 0)).where(
            Trade.account_id == account.id, Trade.executed_at >= _day_start()
        )
    )
    positions = [
        {
            "symbol": p.symbol,
            "quantity": p.quantity,
            "entry_price": p.entry_price,
            "mark_price": p.mark_price,
            "unrealized_pnl": p.unrealized_pnl,
            "leverage": p.leverage,
            "stop_loss": p.stop_loss,
        }
        for p in await open_positions(db, account)
    ]
    equity = eq.equity
    return {
        "source": "paper" if account.execution_mode != ExecutionMode.LIVE else "database",
        "wallet_balance": eq.wallet_balance,
        "available_balance": eq.available_balance,
        "unrealized_pnl": eq.unrealized_pnl,
        "equity": equity,
        "realized_pnl_today": Decimal(str(realized_today or 0)),
        "total_pnl": equity - Decimal(account.paper_initial_balance),
        "open_positions": len(positions),
        "positions": positions,
        "drawdown_pct": _pct(Decimal(account.peak_equity) - equity, account.peak_equity)
        if account.peak_equity
        else 0.0,
    }


async def _exchange_data(client: BinanceFuturesClient) -> dict[str, Any]:
    acc = await client.get_account()
    raw_positions = [p for p in acc.get("positions", []) if Decimal(str(p.get("positionAmt", "0"))) != 0]
    if not raw_positions:
        raw_positions = await client.get_positions()
    start_ms = int(_day_start().timestamp() * 1000)
    realized_today = await client.get_realized_pnl(start_ms)
    positions = [
        {
            "symbol": p["symbol"],
            "quantity": Decimal(str(p.get("positionAmt", "0"))),
            "entry_price": Decimal(str(p.get("entryPrice", "0"))),
            "mark_price": Decimal(str(p["markPrice"])) if p.get("markPrice") else None,
            "unrealized_pnl": Decimal(str(p.get("unrealizedProfit", p.get("unRealizedProfit", "0")))),
            "leverage": int(p.get("leverage", 1)),
            "stop_loss": None,
        }
        for p in raw_positions
    ]
    wallet = Decimal(str(acc.get("totalWalletBalance", "0")))
    unrealized = Decimal(str(acc.get("totalUnrealizedProfit", "0")))
    return {
        "source": "exchange",
        "wallet_balance": wallet,
        "available_balance": Decimal(str(acc.get("availableBalance", "0"))),
        "unrealized_pnl": unrealized,
        "equity": wallet + unrealized,
        "realized_pnl_today": realized_today,
        "total_pnl": None,
        "open_positions": len(positions),
        "positions": positions,
        "drawdown_pct": 0.0,
    }
