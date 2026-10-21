"""Exchange adapters.

- ``base``: the :class:`~app.exchanges.base.ExchangeAdapter` interface and canonical errors.
- ``registry``: per-account factory (``adapter_for_account``) and public market-data factory.
- ``binance_usdm``: Binance USDT-M Futures (default, production).
- ``bybit``: Bybit V5 linear perpetuals (EXPERIMENTAL, disabled by default).

Import the registry explicitly (``from app.exchanges.registry import ...``); this package does not import it
eagerly to avoid circular imports with ``app.services``.
"""

from app.exchanges.base import ExchangeAdapter, ExchangeAPIError, ExchangeCapabilities, UnsupportedFeatureError

__all__ = ["ExchangeAPIError", "ExchangeAdapter", "ExchangeCapabilities", "UnsupportedFeatureError"]
