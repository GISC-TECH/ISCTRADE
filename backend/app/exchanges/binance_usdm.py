"""Binance USDT-M Futures adapter.

The implementation lives in ``app.services.binance_client`` (REST) and ``app.services.binance_ws``
(WebSockets) for backwards compatibility; this module is the adapter's canonical import path.
"""

from app.services.binance_client import BinanceAPIError, BinanceFuturesClient

BinanceUSDMAdapter = BinanceFuturesClient

__all__ = ["BinanceAPIError", "BinanceUSDMAdapter"]
