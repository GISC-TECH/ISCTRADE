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


async def cache_prices(values: dict[str, float | Decimal | str], testnet: bool) -> None:
    """Bulk cache (all-market mark price stream of the real-time feed) in one pipeline round-trip."""
    if not values:
        return
    try:
        pipe = get_redis().pipeline(transaction=False)
        for symbol, price in values.items():
            pipe.set(_key(symbol, testnet), str(price), ex=PRICE_TTL)
        await pipe.execute()
    except Exception as exc:  # pragma: no cover - infra dependent
        logger.debug("price cache unavailable: %s", type(exc).__name__)


async def get_price(symbol: str, testnet: bool) -> Decimal | None:
    try:
        value = await get_redis().get(_key(symbol, testnet))
        if value:
            return Decimal(value)
    except Exception:  # pragma: no cover
        pass
    from app.exchanges.registry import public_adapter

    try:
        async with public_adapter(testnet) as client:
            price = Decimal(str((await client.get_mark_price(symbol))["markPrice"]))
        await cache_price(symbol, price, testnet)
        return price
    except Exception as exc:
        logger.warning("price fetch failed for %s: %s", symbol, exc)
        return None
