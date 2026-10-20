"""Mark price cache (filled by the market stream worker), with REST fallback."""

from __future__ import annotations

import logging
from decimal import Decimal

from app.core.redis import get_redis

logger = logging.getLogger(__name__)
PRICE_TTL = 120


def _key(symbol: str, testnet: bool) -> str:
    return f"price:{'test' if testnet else 'main'}:{symbol.upper()}"


async def cache_price(symbol: str, price: Decimal | str, testnet: bool) -> None:
    try:
        await get_redis().set(_key(symbol, testnet), str(price), ex=PRICE_TTL)
    except Exception as exc:  # pragma: no cover - infra dependent
        logger.debug("price cache unavailable: %s", exc)


async def get_price(symbol: str, testnet: bool) -> Decimal | None:
    try:
        value = await get_redis().get(_key(symbol, testnet))
        if value:
            return Decimal(value)
    except Exception:  # pragma: no cover
        pass
    from app.services.binance_client import BinanceFuturesClient

    try:
        async with BinanceFuturesClient("", "", testnet=testnet) as client:
            price = Decimal(str((await client.get_mark_price(symbol))["markPrice"]))
        await cache_price(symbol, price, testnet)
        return price
    except Exception as exc:
        logger.warning("price fetch failed for %s: %s", symbol, exc)
        return None
