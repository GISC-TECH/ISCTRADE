"""Market scanner for Binance USDT-M perpetuals.

Pipeline (public endpoints only, no API key):

1. ``exchangeInfo`` -> USDT-margined PERPETUAL contracts in TRADING status;
2. ``ticker/24hr`` + ``premiumIndex`` -> liquidity filter (24h quote volume) and funding rate;
3. klines of the ``max_symbols`` most liquid symbols (cached in Redis, memory fallback);
4. per symbol: ADX/DI trend, ATR%, momentum (ROC), volume spike, RSI and the entry signals of the technical
   strategies -> normalised components -> weighted score 0..100.

The Celery beat task ``scanner.run`` stores each snapshot in ``market_scans`` (the API serves the latest one).
A scan is information, not advice: bots in auto mode still pass every order through the RiskManager.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.redis import get_redis
from app.models.scanner import MarketScan
from app.services.binance_client import BinanceFuturesClient
from app.strategy.base import Candle
from app.strategy.indicators import adx, atr, rsi, sma
from app.strategy.strategies import build_strategy, entry_signal

logger = logging.getLogger(__name__)

DEFAULT_WEIGHTS: dict[str, float] = {
    "trend": 0.30,
    "volatility": 0.15,
    "momentum": 0.20,
    "volume": 0.15,
    "funding": 0.05,
    "signals": 0.15,
}
SIGNAL_STRATEGIES = (
    "ema_cross",
    "rsi_mean_reversion",
    "bollinger_reversion",
    "bollinger_squeeze",
    "donchian_breakout",
    "ema_adx_trend",
    "supertrend",
)
KLINE_LIMIT = 150
_memory_cache: dict[str, tuple[float, list]] = {}


@dataclass
class ScanParams:
    interval: str = field(default_factory=lambda: settings.SCANNER_INTERVAL)
    max_symbols: int = field(default_factory=lambda: settings.SCANNER_MAX_SYMBOLS)
    min_quote_volume: float = field(default_factory=lambda: settings.SCANNER_MIN_QUOTE_VOLUME)
    testnet: bool = field(default_factory=lambda: settings.SCANNER_USE_TESTNET)
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    strategies: tuple[str, ...] = SIGNAL_STRATEGIES

    def as_dict(self) -> dict[str, Any]:
        return {
            "interval": self.interval,
            "max_symbols": self.max_symbols,
            "min_quote_volume": self.min_quote_volume,
            "testnet": self.testnet,
            "weights": self.weights,
            "strategies": list(self.strategies),
        }


# --------------------------------------------------------------------------- data


def perpetual_usdt_symbols(exchange_info: dict) -> set[str]:
    return {
        s["symbol"]
        for s in exchange_info.get("symbols", [])
        if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
    }


async def get_klines_cached(client: BinanceFuturesClient, symbol: str, interval: str, limit: int) -> list[Candle]:
    """Closed candles, cached for SCANNER_KLINES_CACHE_SECONDS (Redis, else process memory)."""
    key = f"scanner:klines:{'t' if client.testnet else 'm'}:{symbol}:{interval}:{limit}"
    ttl = settings.SCANNER_KLINES_CACHE_SECONDS
    rows: list | None = None
    try:
        raw = await asyncio.wait_for(get_redis().get(key), timeout=1)
        rows = json.loads(raw) if raw else None
    except Exception:
        hit = _memory_cache.get(key)
        rows = hit[1] if hit and hit[0] > time.monotonic() else None
    if rows is None:
        rows = await client.get_klines(symbol, interval, limit=limit + 1)
        try:
            await asyncio.wait_for(get_redis().set(key, json.dumps(rows), ex=ttl), timeout=1)
        except Exception:
            _memory_cache[key] = (time.monotonic() + ttl, rows)
    now_ms = int(time.time() * 1000)
    return [Candle.from_binance(r) for r in rows if int(r[6]) < now_ms]  # closed candles only


# --------------------------------------------------------------------------- analysis


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def analyse(
    symbol: str,
    candles: list[Candle],
    ticker: dict[str, Any],
    premium: dict[str, Any] | None,
    params: ScanParams,
) -> dict[str, Any] | None:
    if len(candles) < 60:
        return None
    highs, lows, closes = [c.high for c in candles], [c.low for c in candles], [c.close for c in candles]
    vols = [c.volume for c in candles]
    a, pdi, mdi = adx(highs, lows, closes, 14)
    atr_v = atr(highs, lows, closes, 14)[-1]
    r = rsi(closes, 14)[-1]
    vol_avg = sma(vols[:-1], 20)[-1]
    if None in (a[-1], pdi[-1], mdi[-1], atr_v) or not closes[-1]:
        return None
    price = closes[-1]
    atr_pct = atr_v / price * 100  # type: ignore[operator]
    momentum = (price / closes[-21] - 1) * 100 if closes[-21] else 0.0
    spike = vols[-1] / vol_avg if vol_avg else 1.0
    trend = "up" if pdi[-1] > mdi[-1] else "down"  # type: ignore[operator]
    if a[-1] < 20:  # type: ignore[operator]
        trend = "flat"
    funding = float(premium.get("lastFundingRate") or 0) * 100 if premium else 0.0

    signals: dict[str, str] = {}
    for key in params.strategies:
        try:
            sig = entry_signal(build_strategy(key), symbol, candles)
        except Exception:
            sig = None
        if sig:
            signals[key] = sig

    components = {
        "trend": _clip(a[-1] / 50),  # type: ignore[operator]
        "volatility": _clip(atr_pct / 5),
        "momentum": _clip(abs(momentum) / 10),
        "volume": _clip((spike - 1) / 2),
        "funding": _clip(abs(funding) / 0.1),
        "signals": _clip(len(signals) / 2),
    }
    total_w = sum(params.weights.get(k, 0) for k in components) or 1.0
    score = sum(components[k] * params.weights.get(k, 0) for k in components) / total_w * 100
    longs = sum(1 for v in signals.values() if v == "long")
    shorts = len(signals) - longs
    return {
        "symbol": symbol,
        "price": price,
        "change_pct_24h": float(ticker.get("priceChangePercent") or 0),
        "quote_volume": float(ticker.get("quoteVolume") or 0),
        "funding_rate_pct": round(funding, 5),
        "atr_pct": round(atr_pct, 3),
        "adx": round(a[-1], 2),  # type: ignore[arg-type]
        "rsi": round(r, 2) if r is not None else None,
        "trend": trend,
        "momentum_pct": round(momentum, 3),
        "volume_spike": round(spike, 3),
        "signals": signals,
        "bias": "long"
        if longs > shorts
        else "short"
        if shorts > longs
        else ("long" if trend == "up" else "short" if trend == "down" else "neutral"),
        "components": {k: round(v, 4) for k, v in components.items()},
        "score": round(score, 2),
    }


async def run_scan(client: BinanceFuturesClient | None = None, params: ScanParams | None = None) -> dict[str, Any]:
    params = params or ScanParams()
    own = client is None
    client = client or BinanceFuturesClient("", "", testnet=params.testnet)
    started = time.perf_counter()
    errors: list[str] = []
    try:
        info, tickers, premiums = await asyncio.gather(
            client.get_exchange_info(), client.get_24h_tickers(), client.get_premium_index_all()
        )
        universe = perpetual_usdt_symbols(info)
        tick_by = {t["symbol"]: t for t in tickers if t.get("symbol") in universe}
        prem_by = {p["symbol"]: p for p in premiums}
        liquid = sorted(
            (t for t in tick_by.values() if float(t.get("quoteVolume") or 0) >= params.min_quote_volume),
            key=lambda t: float(t.get("quoteVolume") or 0),
            reverse=True,
        )[: params.max_symbols]
        sem = asyncio.Semaphore(5)

        async def one(t: dict[str, Any]) -> dict[str, Any] | None:
            async with sem:
                try:
                    candles = await get_klines_cached(client, t["symbol"], params.interval, KLINE_LIMIT)
                except Exception as exc:
                    errors.append(f"{t['symbol']}: {type(exc).__name__}")
                    return None
            return analyse(t["symbol"], candles, t, prem_by.get(t["symbol"]), params)

        results = [r for r in await asyncio.gather(*(one(t) for t in liquid)) if r]
    finally:
        if own:
            await client.close()
    results.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(results, 1):
        r["rank"] = i
    return {
        "results": results,
        "symbols_scanned": len(results),
        "universe": len(universe),
        "errors": errors[:50],
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "params": params.as_dict(),
    }


# --------------------------------------------------------------------------- persistence / queries


async def run_and_store(
    sessions: async_sessionmaker[AsyncSession], client: BinanceFuturesClient | None = None
) -> dict[str, Any]:
    params = ScanParams()
    out = await run_scan(client, params)
    async with sessions() as db:
        scan = MarketScan(
            testnet=params.testnet,
            interval=params.interval,
            symbols_scanned=out["symbols_scanned"],
            duration_ms=out["duration_ms"],
            params=out["params"],
            results=out["results"],
            errors=out["errors"],
        )
        db.add(scan)
        await db.flush()
        keep = settings.SCANNER_KEEP_SNAPSHOTS
        old = (await db.scalars(select(MarketScan.id).order_by(MarketScan.created_at.desc()).offset(keep))).all()
        if old:
            await db.execute(delete(MarketScan).where(MarketScan.id.in_(old)))
        await db.commit()
    return {"symbols_scanned": out["symbols_scanned"], "duration_ms": out["duration_ms"], "errors": len(out["errors"])}


async def latest_scan(db: AsyncSession) -> MarketScan | None:
    return await db.scalar(select(MarketScan).order_by(MarketScan.created_at.desc()).limit(1))


SORT_FIELDS = {
    "score",
    "quote_volume",
    "change_pct_24h",
    "atr_pct",
    "adx",
    "momentum_pct",
    "volume_spike",
    "funding_rate_pct",
    "rsi",
}


def filter_results(
    results: list[dict[str, Any]],
    *,
    min_quote_volume: float | None = None,
    min_score: float | None = None,
    trend: str | None = None,
    bias: str | None = None,
    signal: str | None = None,
    search: str | None = None,
    sort: str = "score",
    desc: bool = True,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    out = results
    if min_quote_volume:
        out = [r for r in out if r["quote_volume"] >= min_quote_volume]
    if min_score is not None:
        out = [r for r in out if r["score"] >= min_score]
    if trend:
        out = [r for r in out if r["trend"] == trend]
    if bias:
        out = [r for r in out if r["bias"] == bias]
    if signal:
        out = [r for r in out if signal in r["signals"]]
    if search:
        out = [r for r in out if search.upper() in r["symbol"]]
    key = sort if sort in SORT_FIELDS else "score"
    present = sorted((r for r in out if r.get(key) is not None), key=lambda r: r[key], reverse=desc)
    out = present + [r for r in out if r.get(key) is None]  # missing values always last
    return out[:limit] if limit else out


async def top_symbols(db: AsyncSession, n: int, *, min_score: float = 0, strategy_key: str | None = None) -> list[str]:
    """Top-N symbols from the latest scan (optionally only those where `strategy_key` has a signal)."""
    scan = await latest_scan(db)
    if scan is None:
        return []
    rows = [r for r in scan.results if r.get("score", 0) >= min_score]
    if strategy_key:
        with_signal = [r for r in rows if strategy_key in (r.get("signals") or {})]
        rows = with_signal + [r for r in rows if r not in with_signal]
    return [r["symbol"] for r in rows[:n]]
