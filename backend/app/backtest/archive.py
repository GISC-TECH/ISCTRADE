"""Public Binance USDT-M archive (https://data.binance.vision) for backtests.

The archive is static files (no API key, no rate-limit weight) and is reachable from places where the
``fapi`` REST API answers 451 (geo-blocked). Layout used:

* klines:  ``/data/futures/um/monthly/klines/{SYMBOL}/{interval}/{SYMBOL}-{interval}-{YYYY-MM}.zip``,
  and ``/data/futures/um/daily/klines/...-{YYYY-MM-DD}.zip`` for the current month (monthly files are
  published after the month ends);
* funding: ``/data/futures/um/monthly/fundingRate/{SYMBOL}/{SYMBOL}-fundingRate-{YYYY-MM}.zip``
  (``calc_time,funding_interval_hours,last_funding_rate``).

Missing files (404: month not published yet / symbol not listed) are skipped. Downloads are size-capped.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime, timedelta

import httpx

from app.backtest.data import MAX_CANDLES, BacktestDataError, interval_ms, load_csv
from app.backtest.funding import FundingEvent, load_funding_csv
from app.strategy.base import Candle

ARCHIVE_URL = "https://data.binance.vision"
MAX_ZIP_BYTES = 20_000_000
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _months(start: datetime, end: datetime) -> list[tuple[int, int]]:
    y, m = start.year, start.month
    out = []
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


async def _get_csv(client: httpx.AsyncClient, url: str) -> str | None:
    resp = await client.get(url)
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise BacktestDataError(f"arquivo histórico indisponível (HTTP {resp.status_code}): {url}")
    if len(resp.content) > MAX_ZIP_BYTES:
        raise BacktestDataError("arquivo histórico grande demais")
    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            name = next(n for n in zf.namelist() if n.endswith(".csv"))
            info = zf.getinfo(name)
            if info.file_size > MAX_ZIP_BYTES * 5:
                raise BacktestDataError("arquivo histórico grande demais")
            return zf.read(name).decode()
    except (zipfile.BadZipFile, StopIteration, UnicodeDecodeError) as exc:
        raise BacktestDataError(f"arquivo histórico inválido: {url}") from exc


def _client(transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=ARCHIVE_URL, timeout=TIMEOUT, transport=transport, follow_redirects=True)


async def fetch_archive_candles(
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    today: date | None = None,
) -> list[Candle]:
    step = interval_ms(interval)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    if end_ms <= start_ms:
        raise BacktestDataError("end must be after start")
    if (end_ms - start_ms) // step > MAX_CANDLES:
        raise BacktestDataError(f"range too large (max {MAX_CANDLES} candles)")
    today = today or datetime.now(UTC).date()
    candles: dict[int, Candle] = {}
    async with _client(transport) as client:
        for y, m in _months(start, end):
            base = f"/data/futures/um/{{kind}}/klines/{symbol}/{interval}/{symbol}-{interval}-"
            text = None
            if (y, m) != (today.year, today.month):
                text = await _get_csv(client, base.format(kind="monthly") + f"{y:04d}-{m:02d}.zip")
            if text is None:  # current month (or not published yet): daily files
                first = max(date(y, m, 1), start.date())
                last_day = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
                last = min(last_day, end.date(), today - timedelta(days=1))
                parts = []
                for d in _days(first, last) if first <= last else []:
                    part = await _get_csv(client, base.format(kind="daily") + f"{d.isoformat()}.zip")
                    if part:
                        parts.append(part)
                text = _join_csv(parts)
            if text:
                for c in load_csv(_with_header(text), interval):
                    if start_ms <= c.open_time <= end_ms:
                        candles[c.open_time] = c
    return [candles[k] for k in sorted(candles)]


async def fetch_archive_funding(
    symbol: str, start_ms: int, end_ms: int, *, transport: httpx.AsyncBaseTransport | None = None
) -> list[FundingEvent]:
    start = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
    end = datetime.fromtimestamp(end_ms / 1000, tz=UTC)
    events: list[FundingEvent] = []
    async with _client(transport) as client:
        for y, m in _months(start, end):
            url = f"/data/futures/um/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{y:04d}-{m:02d}.zip"
            text = await _get_csv(client, url)
            if text:
                events.extend(e for e in load_funding_csv(text) if start_ms <= e.time <= end_ms)
    return sorted({e.time: e for e in events}.values(), key=lambda e: e.time)


KLINE_HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore"
)


def _with_header(text: str) -> str:
    """Older archive files have no header row."""
    first = text.lstrip().split("\n", 1)[0]
    return text if first.startswith("open_time") else KLINE_HEADER + "\n" + text


def _join_csv(parts: list[str]) -> str | None:
    if not parts:
        return None
    rows: list[str] = []
    for p in parts:
        lines = [ln for ln in p.strip().splitlines() if ln and not ln.startswith("open_time")]
        rows.extend(lines)
    return KLINE_HEADER + "\n" + "\n".join(rows)
