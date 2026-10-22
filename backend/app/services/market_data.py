"""Market data providers used by bots (closed klines and last price)."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Protocol

from app.exchanges.base import ExchangeAdapter
from app.exchanges.registry import public_adapter
from app.strategy.base import Candle, FundingEvent


class MarketData(Protocol):
    async def get_closed_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]: ...

    async def get_price(self, symbol: str) -> Decimal: ...


class BinanceMarketData:
    """Public market data (no API keys needed). Uses testnet or mainnet endpoints."""

    def __init__(self, testnet: bool = True, client: ExchangeAdapter | None = None) -> None:
        self._client = client or public_adapter(testnet)
        self._owns_client = client is None

    async def get_closed_candles(self, symbol: str, interval: str, limit: int = 300) -> list[Candle]:
        rows = await self._client.get_klines(symbol, interval, limit=min(limit + 1, 1500))
        now_ms = int(time.time() * 1000)
        candles = [Candle.from_binance(r) for r in rows]
        return [c for c in candles if c.close_time < now_ms][-limit:]

    async def get_price(self, symbol: str) -> Decimal:
        data = await self._client.get_mark_price(symbol)
        return Decimal(str(data["markPrice"]))

    async def get_funding_history(self, symbol: str, limit: int = 100) -> list[FundingEvent]:
        """Most recent settled funding rates, oldest first (public endpoint)."""
        rows = await self._client.get_funding_rate_history(symbol, limit=min(max(limit, 1), 1000))
        events = [FundingEvent(int(r["fundingTime"]), Decimal(str(r["fundingRate"]))) for r in rows]
        return sorted(events, key=lambda e: e.time)[-limit:]

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()


class StaticMarketData:
    """In-memory provider for tests and paper simulations."""

    def __init__(
        self, candles: dict[str, list[Candle]] | None = None, funding: dict[str, list[FundingEvent]] | None = None
    ) -> None:
        self.candles = candles or {}
        self.funding = funding or {}

    async def get_closed_candles(self, symbol: str, interval: str, limit: int = 300) -> list[Candle]:
        return self.candles.get(symbol, [])[-limit:]

    async def get_price(self, symbol: str) -> Decimal:
        return Decimal(str(self.candles[symbol][-1].close))

    async def get_funding_history(self, symbol: str, limit: int = 100) -> list[FundingEvent]:
        return sorted(self.funding.get(symbol, []), key=lambda e: e.time)[-limit:]

    async def close(self) -> None:
        return None
