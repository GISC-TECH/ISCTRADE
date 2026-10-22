"""Funding-rate history for backtests (F-08).

Sources, in the order ``mode="auto"`` tries them:

1. the exchange REST API (``ExchangeAdapter.get_funding_rate_history``, paged);
2. the public Binance archive ``data.binance.vision`` (monthly ``fundingRate`` files; works where the REST
   API is geo-blocked) - Binance mainnet only, see app/backtest/archive.py;
3. a CSV uploaded with the request (``fundingTime,fundingRate`` or the archive's
   ``calc_time,funding_interval_hours,last_funding_rate``);
4. a constant rate every 8 h (00/08/16 UTC) - a rough model when no history exists.

Accounting (perpetual futures convention): at each funding timestamp the position *held at that moment* pays
``qty * mark * rate`` (long pays when the rate is positive, short receives; the opposite when negative).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.backtest.data import BacktestDataError, _ts
from app.exchanges.base import ExchangeAdapter
from app.strategy.base import FundingEvent

__all__ = ["FundingEvent", "constant_funding", "fetch_funding", "funding_summary", "load_funding_csv", "normalize"]

EIGHT_HOURS_MS = 8 * 3_600_000
MAX_FUNDING_EVENTS = 20_000  # ~18 years of 8h funding
PAGE_LIMIT = 1000


def normalize(
    rows: Iterable[dict[str, Any]], start_ms: int | None = None, end_ms: int | None = None
) -> list[FundingEvent]:
    """Exchange rows (``fundingTime``/``fundingRate``) -> sorted, de-duplicated events inside [start, end]."""
    seen: dict[int, FundingEvent] = {}
    for r in rows:
        try:
            t = int(r.get("fundingTime") if r.get("fundingTime") is not None else r["time"])
            rate = Decimal(str(r.get("fundingRate") if r.get("fundingRate") is not None else r["rate"]))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        if (start_ms is not None and t < start_ms) or (end_ms is not None and t > end_ms):
            continue
        seen[t] = FundingEvent(t, rate)
    return [seen[t] for t in sorted(seen)]


def load_funding_csv(text: str) -> list[FundingEvent]:
    reader = csv.DictReader(io.StringIO((text or "").strip()))
    if not reader.fieldnames:
        raise BacktestDataError("CSV de funding vazio")
    fields = {f.strip().lower(): f for f in reader.fieldnames}
    tcol = fields.get("fundingtime") or fields.get("funding_time") or fields.get("calc_time") or fields.get("time")
    rcol = (
        fields.get("fundingrate") or fields.get("funding_rate") or fields.get("last_funding_rate") or fields.get("rate")
    )
    if not tcol or not rcol:
        raise BacktestDataError("CSV de funding precisa das colunas fundingTime,fundingRate")
    out: list[dict[str, Any]] = []
    for n, row in enumerate(reader, start=2):
        try:
            out.append({"fundingTime": _ts(row[tcol]), "fundingRate": Decimal(row[rcol].strip())})
        except (TypeError, ValueError, InvalidOperation, AttributeError) as exc:
            raise BacktestDataError(f"linha {n} inválida no CSV de funding: {exc}") from exc
        if len(out) > MAX_FUNDING_EVENTS:
            raise BacktestDataError(f"funding demais (máx. {MAX_FUNDING_EVENTS})")
    return normalize(out)


def constant_funding(start_ms: int, end_ms: int, rate: Decimal, every_ms: int = EIGHT_HOURS_MS) -> list[FundingEvent]:
    first = -(-start_ms // every_ms) * every_ms  # first multiple of 8h (00/08/16 UTC) >= start
    return [FundingEvent(t, rate) for t in range(first, end_ms + 1, every_ms)][:MAX_FUNDING_EVENTS]


async def fetch_funding(client: ExchangeAdapter, symbol: str, start_ms: int, end_ms: int) -> list[FundingEvent]:
    """Page through the exchange's funding history (oldest first)."""
    rows: list[dict[str, Any]] = []
    cursor = start_ms
    while cursor <= end_ms and len(rows) < MAX_FUNDING_EVENTS:
        page = await client.get_funding_rate_history(symbol, start_time=cursor, end_time=end_ms, limit=PAGE_LIMIT)
        if not page:
            break
        rows.extend(page)
        last = max(int(r.get("fundingTime") or 0) for r in page)
        if last < cursor or len(page) < min(PAGE_LIMIT, 200):  # Bybit pages are capped at 200
            break
        cursor = last + 1
    return normalize(rows, start_ms, end_ms)


def funding_summary(events: list[FundingEvent], source: str) -> dict[str, Any]:
    if not events:
        return {"funding_source": source, "funding_events": 0}
    rates = [e.rate for e in events]
    return {
        "funding_source": source,
        "funding_events": len(events),
        "funding_avg_rate": float(sum(rates) / len(rates)),
        "funding_first": datetime.fromtimestamp(events[0].time / 1000, tz=UTC).isoformat(),
        "funding_last": datetime.fromtimestamp(events[-1].time / 1000, tz=UTC).isoformat(),
    }
