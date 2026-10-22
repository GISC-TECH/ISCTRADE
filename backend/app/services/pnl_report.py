"""Realized PnL by period (day/week/month) grouped by account/bot/strategy/symbol, and CSV exports.

Aggregation runs in Python over the selected fills so it is identical on PostgreSQL and SQLite and buckets use
UTC calendar boundaries (weeks start on Monday, ISO 8601).
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, BotConfig, Trade
from app.models.base import as_utc, utcnow

Period = Literal["day", "week", "month"]
GroupBy = Literal["none", "account", "bot", "strategy", "symbol"]
USD_FEE_ASSETS = {"USDT", "USDC", "BUSD", "USD"}
DEFAULT_LOOKBACK = {"day": timedelta(days=30), "week": timedelta(weeks=26), "month": timedelta(days=366)}
MAX_RANGE = timedelta(days=3 * 366)
MANUAL = "manual"


def bucket_start(ts: datetime, period: Period) -> date:
    d = as_utc(ts).date()  # type: ignore[union-attr]
    if period == "week":
        return d - timedelta(days=d.weekday())
    if period == "month":
        return d.replace(day=1)
    return d


@dataclass
class Totals:
    realized_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")  # fees paid in USD stablecoins (USDT-M); other assets in fees_other
    trades: int = 0
    fees_other: dict[str, Decimal] = field(default_factory=dict)

    def add(self, pnl: Decimal, fee: Decimal, fee_asset: str) -> None:
        self.realized_pnl += pnl
        self.trades += 1
        if (fee_asset or "USDT").upper() in USD_FEE_ASSETS:
            self.fees += fee
        else:
            self.fees_other[fee_asset] = self.fees_other.get(fee_asset, Decimal("0")) + fee

    def as_dict(self) -> dict:
        return {
            "realized_pnl": self.realized_pnl,
            "fees": self.fees,
            "net_pnl": self.realized_pnl - self.fees,
            "trades": self.trades,
            "fees_other": {k: v for k, v in self.fees_other.items()},
        }


def resolve_range(period: Period, start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    end = as_utc(end) if end else utcnow()
    start = as_utc(start) if start else end - DEFAULT_LOOKBACK[period]
    if start >= end:  # type: ignore[operator]
        raise ValueError("Início deve ser anterior ao fim")
    if end - start > MAX_RANGE:  # type: ignore[operator]
        raise ValueError("Intervalo máximo: 3 anos")
    return start, end  # type: ignore[return-value]


async def _labels(db: AsyncSession, workspace_id: uuid.UUID) -> tuple[dict, dict]:
    accounts = {a.id: a for a in await db.scalars(select(Account).where(Account.workspace_id == workspace_id))}
    bots = {b.id: b for b in await db.scalars(select(BotConfig).where(BotConfig.workspace_id == workspace_id))}
    return accounts, bots


def _trade_stmt(workspace_id: uuid.UUID, start: datetime, end: datetime, account_id: uuid.UUID | None):
    stmt = select(Trade).where(Trade.workspace_id == workspace_id, Trade.executed_at >= start, Trade.executed_at < end)
    if account_id:
        stmt = stmt.where(Trade.account_id == account_id)
    return stmt.order_by(Trade.executed_at)


def _group_key(t: Trade, group_by: GroupBy, accounts: dict, bots: dict) -> tuple[str, str]:
    if group_by == "account":
        acc = accounts.get(t.account_id)
        return str(t.account_id), acc.label if acc else "—"
    if group_by == "bot":
        bot = bots.get(t.bot_id) if t.bot_id else None
        return (str(t.bot_id), bot.name) if bot else (MANUAL, "Manual / sem bot")
    if group_by == "strategy":
        bot = bots.get(t.bot_id) if t.bot_id else None
        return (bot.strategy_key, bot.strategy_key) if bot else (MANUAL, "Manual / sem bot")
    if group_by == "symbol":
        return t.symbol, t.symbol
    return "all", "Total"


async def pnl_by_period(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    period: Period,
    group_by: GroupBy,
    start: datetime,
    end: datetime,
    account_id: uuid.UUID | None = None,
) -> dict:
    accounts, bots = await _labels(db, workspace_id)
    buckets: dict[tuple[date, str], Totals] = {}
    groups: dict[str, Totals] = {}
    labels: dict[str, str] = {}
    total = Totals()
    stmt = _trade_stmt(workspace_id, start, end, account_id).execution_options(yield_per=1000)
    result = await db.stream_scalars(stmt)
    async for t in result:
        key, label = _group_key(t, group_by, accounts, bots)
        labels[key] = label
        b = bucket_start(t.executed_at, period)
        for agg in (buckets.setdefault((b, key), Totals()), groups.setdefault(key, Totals()), total):
            agg.add(t.realized_pnl, t.fee, t.fee_asset)
    return {
        "period": period,
        "group_by": group_by,
        "start": start,
        "end": end,
        "buckets": [
            {"period_start": b, "group": key, "label": labels[key], **agg.as_dict()}
            for (b, key), agg in sorted(buckets.items(), key=lambda kv: (kv[0][0], kv[0][1]))
        ],
        "groups": sorted(
            ({"group": key, "label": labels[key], **agg.as_dict()} for key, agg in groups.items()),
            key=lambda g: g["net_pnl"],
            reverse=True,
        ),
        "totals": total.as_dict(),
    }


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_text(value: object) -> str:
    """Neutralize spreadsheet formula injection in free-text cells (labels, bot names)."""
    s = "" if value is None else str(value)
    return "'" + s if s.startswith(_FORMULA_PREFIXES) else s


def _iso(ts: datetime) -> str:
    return as_utc(ts).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # type: ignore[union-attr]


class _Writer:
    def __init__(self, delimiter: str):
        self.buf = io.StringIO()
        self.w = csv.writer(self.buf, delimiter=delimiter, lineterminator="\r\n")

    def row(self, values: list) -> str:
        self.w.writerow(values)
        out = self.buf.getvalue()
        self.buf.seek(0)
        self.buf.truncate(0)
        return out


TRADE_COLUMNS = [
    "data_hora_utc", "conta", "exchange", "modo", "testnet", "bot", "estrategia", "simbolo", "lado", "quantidade",
    "preco", "notional_usdt", "taxa", "ativo_taxa", "pnl_realizado_usdt", "id_ordem", "id_trade_exchange", "id_trade",
]  # fmt: skip
INCOME_COLUMNS = [
    "periodo_inicio", "conta", "exchange", "modo", "pnl_realizado_usdt", "taxas_usdt", "pnl_liquido_usdt",
    "operacoes", "taxas_outros_ativos",
]  # fmt: skip


async def trades_csv(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    start: datetime,
    end: datetime,
    account_id: uuid.UUID | None = None,
    delimiter: str = ",",
) -> AsyncIterator[str]:
    accounts, bots = await _labels(db, workspace_id)
    w = _Writer(delimiter)
    yield "\ufeff" + w.row(TRADE_COLUMNS)  # BOM: Excel opens UTF-8 (accents) correctly
    stmt = _trade_stmt(workspace_id, start, end, account_id).execution_options(yield_per=1000)
    result = await db.stream_scalars(stmt)
    async for t in result:
        acc = accounts.get(t.account_id)
        bot = bots.get(t.bot_id) if t.bot_id else None
        yield w.row([
            _iso(t.executed_at), safe_text(acc.label if acc else ""), acc.exchange if acc else "",
            "paper" if t.is_paper else "live", (acc.is_testnet if acc else ""), safe_text(bot.name if bot else ""),
            bot.strategy_key if bot else MANUAL, t.symbol, t.side.value, t.quantity, t.price, t.price * t.quantity,
            t.fee, t.fee_asset, t.realized_pnl, t.order_id or "", t.exchange_trade_id or "", t.id,
        ])  # fmt: skip


async def income_csv(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    period: Period,
    start: datetime,
    end: datetime,
    account_id: uuid.UUID | None = None,
    delimiter: str = ",",
) -> AsyncIterator[str]:
    report = await pnl_by_period(db, workspace_id, period, "account", start, end, account_id)
    accounts, _ = await _labels(db, workspace_id)
    w = _Writer(delimiter)
    yield "\ufeff" + w.row(INCOME_COLUMNS)
    for b in report["buckets"]:
        acc = accounts.get(uuid.UUID(b["group"])) if b["group"] not in ("all", MANUAL) else None
        other = " ".join(f"{k}:{v}" for k, v in sorted(b["fees_other"].items()))
        yield w.row([
            b["period_start"].isoformat(), safe_text(b["label"]), acc.exchange if acc else "",
            ("live" if acc.execution_mode.value == "live" else "paper") if acc else "", b["realized_pnl"], b["fees"],
            b["net_pnl"], b["trades"], other,
        ])  # fmt: skip
