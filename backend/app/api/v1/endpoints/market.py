"""Public market data for the real-time chart (initial candles; live updates come over the WebSocket)."""

import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import WorkspaceContext, get_workspace_context
from app.exchanges.base import ExchangeAdapter
from app.exchanges.registry import public_adapter


def market_client(testnet: bool) -> ExchangeAdapter:
    """Public (unsigned) adapter of the default exchange; replaced in tests."""
    return public_adapter(testnet)


router = APIRouter()
Interval = Literal["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"]


@router.get("/klines")
async def klines(
    symbol: str = Query(min_length=2, max_length=20, pattern=r"^[A-Za-z0-9]+$"),
    interval: Interval = "1m",
    limit: int = Query(default=500, ge=10, le=1000),
    testnet: bool = False,
    _: WorkspaceContext = Depends(get_workspace_context),
) -> list[dict]:
    """Candles `{t, o, h, l, c, v, x}` (t = open time in ms, x = candle closed)."""
    try:
        async with market_client(testnet) as client:
            rows = await client.get_klines(symbol.upper(), interval, limit=limit)
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Falha ao obter candles: {type(exc).__name__}") from exc
    now = int(time.time() * 1000)
    return [
        {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5]),
         "x": int(r[6]) < now}
        for r in rows
    ]  # fmt: skip
