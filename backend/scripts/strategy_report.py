"""Indicative backtest report for the bot presets and classic strategies (NOT a guarantee of future results).

Usage (from backend/):
    python -m scripts.strategy_report --source vision --end 2026-09-30 --out report.md   # mainnet archive
    python -m scripts.strategy_report --source testnet

Data sources: ``vision`` = monthly mainnet kline archives from data.binance.vision (public, works where the REST
API is geo-blocked), ``mainnet``/``testnet`` = public REST klines (Testnet prices contain artificial spikes and are
NOT representative). No API keys are used; runs the same BacktestEngine/RiskManager as the platform with the default
costs (taker fee 0.04%, slippage 2 bps, risk 1%/trade, 15% drawdown kill switch). Funding is not modelled.
Never sends orders.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import zipfile
from datetime import UTC, datetime, timedelta

import httpx

from app.backtest.data import fetch_candles
from app.backtest.engine import BacktestConfig, run_backtest
from app.services.binance_client import BinanceFuturesClient
from app.strategy.base import Candle
from app.strategy.classic import CLASSIC_STRATEGY_KEYS
from app.strategy.presets import PRESETS
from app.strategy.strategies import build_strategy

PERIOD_DAYS = {"5m": 30, "15m": 90, "1h": 365, "4h": 730, "1d": 1095}
STRATEGY_INTERVALS = {"session_breakout": "15m", "vwap_reversion": "15m", "turtle": "1d", "volatility_breakout": "1d"}


VISION = "https://data.binance.vision/data/futures/um/monthly/klines/{s}/{i}/{s}-{i}-{y:04d}-{m:02d}.zip"


async def fetch_vision(http: httpx.AsyncClient, symbol: str, interval: str, start: datetime, end: datetime):
    """Closed candles from the monthly archives of data.binance.vision (USDT-M futures, mainnet)."""
    out: list[Candle] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        r = await http.get(VISION.format(s=symbol, i=interval, y=y, m=m))
        if r.status_code == 200:
            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                text = zf.read(zf.namelist()[0]).decode()
            for row in csv.reader(io.StringIO(text)):
                if row and row[0].isdigit():
                    out.append(Candle.from_binance(row))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    lo, hi = start.timestamp() * 1000, end.timestamp() * 1000
    return [c for c in out if lo <= c.open_time and c.close_time <= hi]


async def load(client, symbol: str, interval: str, end: datetime, cache: dict):
    key = (symbol, interval)
    if key not in cache:
        start = end - timedelta(days=PERIOD_DAYS[interval])
        if isinstance(client, httpx.AsyncClient):
            cache[key] = await fetch_vision(client, symbol, interval, start, end)
        else:
            cache[key] = await fetch_candles(client, symbol, interval, start, end)
    return cache[key]


def row(label: str, strategy_key: str, params: dict, candles, symbol: str, interval: str, leverage: int) -> str:
    cfg = BacktestConfig(leverage=leverage)
    res = run_backtest(build_strategy(strategy_key, params), candles, symbol, interval, cfg)
    m = res.metrics
    pf = "—" if m["profit_factor"] is None else f"{m['profit_factor']:.2f}"
    killed = " (kill switch)" if res.killed_at else ""
    return (
        f"| {label} | {symbol} {interval} | {m['start'][:10]} → {m['end'][:10]} | {m['num_closed_trades']} | "
        f"{m['return_pct']:+.2f}%{killed} | {m['max_drawdown_pct']:.2f}% | {m['win_rate_pct']:.0f}% | {pf} | "
        f"{m['buy_hold_return_pct']:+.1f}% | {sum(res.rejections.values())} |"
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=("vision", "mainnet", "testnet"), default="vision")
    ap.add_argument("--end", default=datetime.now(UTC).strftime("%Y-%m-%d"))
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC) + timedelta(days=1)  # inclusive end date
    head = (
        "| Robô / estratégia | Mercado | Período | Trades | Retorno | DD máx. | Acerto | Profit factor | Buy & hold "
        "| Rejeições |\n|---|---|---|---|---|---|---|---|---|---|"
    )
    source = {"vision": "data.binance.vision (mainnet)", "mainnet": "API mainnet", "testnet": "API Testnet"}
    lines = [f"Fonte: {source[args.source]} — gerado em {datetime.now(UTC):%Y-%m-%d}", ""]
    cache: dict = {}
    if args.source == "vision":
        ctx = httpx.AsyncClient(timeout=60)
    else:
        ctx = BinanceFuturesClient(api_key="", api_secret="", testnet=args.source == "testnet")
    async with ctx as client:
        lines += ["### Robôs prontos", "", head]
        for p in PRESETS:
            candles = await load(client, p.symbol, p.interval, end, cache)
            lines.append(row(p.name, p.strategy_key, p.params, candles, p.symbol, p.interval, p.leverage))
        lines += ["", "### Estratégias clássicas (parâmetros padrão, BTCUSDT)", "", head]
        for key in CLASSIC_STRATEGY_KEYS:
            interval = STRATEGY_INTERVALS.get(key, "4h")
            candles = await load(client, "BTCUSDT", interval, end, cache)
            leverage = 3 if interval in ("5m", "15m") else 1
            lines.append(row(f"`{key}` ({leverage}x)", key, {}, candles, "BTCUSDT", interval, leverage))
    text = "\n".join(lines) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)


if __name__ == "__main__":
    asyncio.run(main())
