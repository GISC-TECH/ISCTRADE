"""Aggregate risk limits per workspace and for the whole instance (G-02), plus the live-account capital cap.

Limits (0/None = disabled):

* ``max_total_notional_usdt`` - sum of |qty| x mark of every open position in the scope (after the order);
* ``max_daily_loss_usdt``     - sum of (day-start equity - current equity) of the scope's accounts;
* ``max_open_positions``      - concurrent open positions (account x symbol) in the scope.

Scopes: the order's workspace and the whole instance ("global"). Only LIVE accounts are counted (and guarded)
unless ``RISK_AGGREGATE_INCLUDE_PAPER=true`` (then paper accounts are aggregated separately from live ones).

Storage: ``SystemSetting`` rows ``risk_limits:ws:<workspace id>`` and ``risk_limits:global``; without a row the
environment defaults (``RISK_WS_*`` / ``RISK_GLOBAL_*``) apply. Who may change what is enforced by the API:
traders can only tighten, workspace admins may go back up to the environment defaults, only the platform
super-admin can go above them or change the global limits.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Account, ExecutionMode, Position
from app.models.system import SystemSetting
from app.services.risk_manager import AggregateExposure, OrderRequest, Side

GLOBAL_KEY = "risk_limits:global"
FIELDS = ("max_total_notional_usdt", "max_daily_loss_usdt", "max_open_positions")


def workspace_key(workspace_id: uuid.UUID | str) -> str:
    return f"risk_limits:ws:{workspace_id}"


def _opt_dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    d = Decimal(str(value))
    return d if d > 0 else None


def _opt_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    i = int(value)
    return i if i > 0 else None


@dataclass(frozen=True)
class AggregateLimits:
    max_total_notional_usdt: Decimal | None = None
    max_daily_loss_usdt: Decimal | None = None
    max_open_positions: int | None = None

    @classmethod
    def build(cls, notional: Any, daily_loss: Any, positions: Any) -> AggregateLimits:
        return cls(_opt_dec(notional), _opt_dec(daily_loss), _opt_int(positions))

    @classmethod
    def from_dict(cls, data: dict[str, Any], fallback: AggregateLimits) -> AggregateLimits:
        merged = {**asdict(fallback), **{k: data[k] for k in FIELDS if k in data}}
        return cls.build(*(merged[k] for k in FIELDS))

    def to_json(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}

    @property
    def enabled(self) -> bool:
        return any(v is not None for v in asdict(self).values())

    def loosened_fields(self, baseline: AggregateLimits) -> list[str]:
        """Fields where `self` is more permissive than `baseline` (higher, or disabled while baseline is set)."""
        out = []
        for k in FIELDS:
            new, base = getattr(self, k), getattr(baseline, k)
            if base is not None and (new is None or new > base):
                out.append(k)
        return out


def default_workspace_limits() -> AggregateLimits:
    return AggregateLimits.build(
        settings.RISK_WS_MAX_TOTAL_NOTIONAL_USDT, settings.RISK_WS_MAX_DAILY_LOSS_USDT,
        settings.RISK_WS_MAX_OPEN_POSITIONS,
    )  # fmt: skip


def default_global_limits() -> AggregateLimits:
    return AggregateLimits.build(
        settings.RISK_GLOBAL_MAX_TOTAL_NOTIONAL_USDT, settings.RISK_GLOBAL_MAX_DAILY_LOSS_USDT,
        settings.RISK_GLOBAL_MAX_OPEN_POSITIONS,
    )  # fmt: skip


async def _get(db: AsyncSession, key: str, fallback: AggregateLimits) -> AggregateLimits:
    row = await db.get(SystemSetting, key)
    return AggregateLimits.from_dict(row.value or {}, fallback) if row is not None else fallback


async def _set(db: AsyncSession, key: str, limits: AggregateLimits) -> None:
    row = await db.get(SystemSetting, key)
    if row is None:
        db.add(SystemSetting(key=key, value=limits.to_json()))
    else:
        row.value = limits.to_json()


async def get_workspace_limits(db: AsyncSession, workspace_id: uuid.UUID) -> AggregateLimits:
    return await _get(db, workspace_key(workspace_id), default_workspace_limits())


async def set_workspace_limits(db: AsyncSession, workspace_id: uuid.UUID, limits: AggregateLimits) -> None:
    await _set(db, workspace_key(workspace_id), limits)


async def get_global_limits(db: AsyncSession) -> AggregateLimits:
    return await _get(db, GLOBAL_KEY, default_global_limits())


async def set_global_limits(db: AsyncSession, limits: AggregateLimits) -> None:
    await _set(db, GLOBAL_KEY, limits)


def instance_capital_cap() -> Decimal | None:
    return _opt_dec(settings.RISK_LIVE_ACCOUNT_MAX_CAPITAL_USDT)


def capital_cap(account: Account) -> Decimal | None:
    """Hard capital cap of a LIVE account: the lowest of the account cap and the instance cap (None = none)."""
    if account.execution_mode != ExecutionMode.LIVE:
        return None
    caps = [c for c in (_opt_dec(account.max_capital_usdt), instance_capital_cap()) if c is not None]
    return min(caps) if caps else None


def _mode_filter(live: bool):
    return Account.execution_mode == ExecutionMode.LIVE if live else Account.execution_mode != ExecutionMode.LIVE


async def aggregate_exposures(
    db: AsyncSession, account: Account, order: OrderRequest, current_equity: Decimal
) -> list[AggregateExposure]:
    """Projected exposure of the order's workspace and of the instance (only scopes with limits enabled)."""
    live = account.execution_mode == ExecutionMode.LIVE
    if order.reduce_only or (not live and not settings.RISK_AGGREGATE_INCLUDE_PAPER):
        return []
    scopes = [
        ("workspace", await get_workspace_limits(db, account.workspace_id), account.workspace_id),
        ("global", await get_global_limits(db), None),
    ]
    out: list[AggregateExposure] = []
    for scope, limits, ws_id in scopes:
        if not limits.enabled:
            continue
        pos_q = (
            select(Position)
            .join(Account, Account.id == Position.account_id)
            .where(Position.quantity != 0, _mode_filter(live))
        )
        acc_q = select(Account).where(_mode_filter(live))
        if ws_id is not None:
            pos_q = pos_q.where(Account.workspace_id == ws_id)
            acc_q = acc_q.where(Account.workspace_id == ws_id)
        others_notional, others_count, q0 = Decimal("0"), 0, Decimal("0")
        for p in await db.scalars(pos_q):
            if p.account_id == account.id and p.symbol == order.symbol:
                q0 = Decimal(p.quantity)
                continue
            others_notional += abs(Decimal(p.quantity)) * Decimal(p.mark_price or p.entry_price or 0)
            others_count += 1
        delta = order.quantity if order.side == Side.BUY else -order.quantity
        q1 = q0 + delta
        daily_loss = Decimal("0")
        for acc in await db.scalars(acc_q):
            if acc.day_start_equity is None:
                continue
            equity = current_equity if acc.id == account.id else acc.last_equity
            if equity is not None:
                daily_loss += Decimal(acc.day_start_equity) - Decimal(equity)
        out.append(
            AggregateExposure(
                scope=scope,
                notional=others_notional + abs(q1) * order.entry_price,
                open_positions=others_count + (1 if q1 != 0 else 0),
                daily_loss=daily_loss,
                max_total_notional=limits.max_total_notional_usdt,
                max_daily_loss=limits.max_daily_loss_usdt,
                max_open_positions=limits.max_open_positions,
                adds_notional=abs(q1) > abs(q0),
                opens_position=q0 == 0 and q1 != 0,
            )
        )
    return out
