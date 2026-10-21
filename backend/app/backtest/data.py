"""Historical data loading for backtests (Binance klines or CSV)."""

from __future__ import annotations

import csv
import io
from datetime import datetime

from app.exchanges.base import ExchangeAdapter
from app.strategy.base import Candle

INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
}

MAX_CANDLES = 50_000


class BacktestDataError(ValueError):
    pass


def interval_ms(interval: str) -> int:
    try:
        return INTERVAL_MS[interval]
    except KeyError:
        raise BacktestDataError(f"unsupported interval '{interval}'") from None


def _ts(value: str) -> int:
    value = value.strip()
    if value.isdigit():
        n = int(value)
        return n * 1000 if n < 10_000_000_000 else n  # seconds -> ms
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def load_csv(text: str, interval: str = "1h") -> list[Candle]:
    """CSV with header: open_time,open,high,low,close,volume[,close_time]. Times as ms/s epoch or ISO-8601."""
    step = interval_ms(interval)
    reader = csv.DictReader(io.StringIO(text.strip()))
    if not reader.fieldnames:
        raise BacktestDataError("empty CSV")
    fields = {f.strip().lower(): f for f in reader.fieldnames}
    required = {"open_time", "open", "high", "low", "close"}
    missing = required - set(fields)
    if missing:
        raise BacktestDataError(f"missing CSV columns: {', '.join(sorted(missing))}")
    candles: list[Candle] = []
    for n, row in enumerate(reader, start=2):
        try:
            ot = _ts(row[fields["open_time"]])
            ct = _ts(row[fields["close_time"]]) if "close_time" in fields else ot + step - 1
            candles.append(
                Candle(
                    open_time=ot,
                    open=float(row[fields["open"]]),
                    high=float(row[fields["high"]]),
                    low=float(row[fields["low"]]),
                    close=float(row[fields["close"]]),
                    volume=float(row[fields["volume"]]) if "volume" in fields else 0.0,
                    close_time=ct,
                )
            )
        except (TypeError, ValueError) as exc:
            raise BacktestDataError(f"invalid CSV row {n}: {exc}") from exc
        if len(candles) > MAX_CANDLES:
            raise BacktestDataError(f"too many candles (max {MAX_CANDLES})")
    candles.sort(key=lambda c: c.open_time)
    return candles


async def fetch_candles(
    client: ExchangeAdapter, symbol: str, interval: str, start: datetime, end: datetime
) -> list[Candle]:
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    if end_ms <= start_ms:
        raise BacktestDataError("end must be after start")
    if (end_ms - start_ms) // interval_ms(interval) > MAX_CANDLES:
        raise BacktestDataError(f"range too large (max {MAX_CANDLES} candles)")
    rows = await client.get_klines_range(symbol, interval, start_ms, end_ms)
    return [Candle.from_binance(r) for r in rows]
