"""Collects DB-derived gauges for the API's /metrics endpoint."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, BotConfig, Position
from app.services.execution import is_global_kill_switch_on
from app.services.health import streams_heartbeat_age


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


async def collect(db: AsyncSession) -> dict[str, Any]:
    bots = {str(getattr(s, "value", s)): n for s, n in (await db.execute(
        select(BotConfig.state, func.count()).group_by(BotConfig.state)
    )).all()}  # fmt: skip
    positions = dict(
        (await db.execute(
            select(Position.account_id, func.count()).where(Position.quantity != 0).group_by(Position.account_id)
        )).all()
    )  # fmt: skip
    accounts: dict[tuple[str, str], int] = {}
    rows = []
    for acc in await db.scalars(select(Account)):
        status, mode = str(acc.status.value), str(acc.execution_mode.value)
        accounts[(status, mode)] = accounts.get((status, mode), 0) + 1
        equity = Decimal(acc.last_equity) if acc.last_equity is not None else None
        if equity is None and mode == "paper":
            equity = Decimal(acc.paper_balance)
        daily = equity - Decimal(acc.day_start_equity) if equity is not None and acc.day_start_equity else None
        dd = None
        if equity is not None and acc.peak_equity and Decimal(acc.peak_equity) > 0:
            dd = max(Decimal(0), (Decimal(acc.peak_equity) - equity) / Decimal(acc.peak_equity) * 100)
        rows.append({
            "id": str(acc.id), "workspace_id": str(acc.workspace_id), "mode": mode,
            "equity": _f(equity), "daily_pnl": _f(daily), "drawdown_pct": _f(dd),
            "open_positions": positions.get(acc.id, 0),
        })  # fmt: skip
    return {
        "bots": bots,
        "accounts": accounts,
        "account_rows": rows,
        "global_kill": await is_global_kill_switch_on(db),
        "streams_heartbeat_age": await streams_heartbeat_age(),
    }
