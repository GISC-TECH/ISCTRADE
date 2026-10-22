"""Exchange adapter interface.

Every service that talks to an exchange (execution, reconciler, equity, dashboard, scanner, backtest,
streams, kill switch) depends on :class:`ExchangeAdapter`, never on a concrete client.

Canonical wire format
---------------------
To keep the refactor incremental (and behavior identical for Binance), the *canonical* payloads exchanged
between adapters and services are the **Binance USDT-M Futures** shapes, which the codebase already
understands:

* klines: ``[open_time_ms, open, high, low, close, volume, close_time_ms, ...]`` (oldest first);
* exchange info: ``{"symbols": [{"symbol", "status": "TRADING", "contractType": "PERPETUAL",
  "quoteAsset": "USDT", "filters": [...]}]}``;
* 24h tickers: ``{"symbol", "lastPrice", "priceChangePercent", "quoteVolume", ...}``;
* premium index / funding: ``{"symbol", "markPrice", "indexPrice", "lastFundingRate", "nextFundingTime"}``;
* account: ``{"totalWalletBalance", "availableBalance", "totalUnrealizedProfit", "canTrade", ...}``;
* positions (``positionRisk``): ``{"symbol", "positionAmt" (signed), "entryPrice", "markPrice",
  "unRealizedProfit", "leverage", "liquidationPrice"}``;
* orders: ``{"orderId", "clientOrderId", "symbol", "status" (NEW/PARTIALLY_FILLED/FILLED/CANCELED/
  REJECTED/EXPIRED), "side" (BUY/SELL), "type", "origQty", "executedQty", "avgPrice", "price"}``;
* user-data events: ``ORDER_TRADE_UPDATE`` / ``ACCOUNT_UPDATE`` / ``listenKeyExpired``.

Adapters for other venues translate their native payloads into these shapes (and their native error codes
into the canonical codes below). Nothing outside ``app/exchanges`` should know which venue is behind it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar, Protocol

from app.services.symbol_filters import SymbolFilters

StreamHandler = Callable[[dict[str, Any]], Awaitable[None]]

# Canonical error codes (Binance numbering) that services branch on.
ERR_INVALID_SYMBOL = -1121
ERR_CANCEL_REJECTED = -2011
ERR_ORDER_NOT_FOUND = -2013
ERR_NO_CHANGE = -4046  # margin type / leverage / position mode already set


class ExchangeAPIError(Exception):
    """Error returned by an exchange API, normalized to (http status, canonical code, message)."""

    def __init__(self, status_code: int, code: int | None, message: str):
        super().__init__(f"[{status_code}] {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


class UnsupportedFeatureError(ExchangeAPIError):
    """The adapter (or the venue) does not support this operation."""

    def __init__(self, feature: str, exchange: str):
        super().__init__(501, None, f"{feature} not supported by {exchange}")
        self.feature = feature


@dataclass(frozen=True)
class ExchangeCapabilities:
    """Static description of what an adapter supports (used by the registry, API and docs)."""

    perpetual_futures: bool = True
    testnet: bool = True
    hedge_mode: bool = False
    user_data_stream: bool = True
    market_streams: bool = True
    test_order: bool = False
    quote_assets: tuple[str, ...] = ("USDT",)
    notes: tuple[str, ...] = field(default_factory=tuple)


class ExchangeStream(Protocol):
    """A long-running WebSocket stream with reconnection (see ``app.services.binance_ws.BinanceStream``)."""

    name: str
    alert_context: dict

    async def run(self) -> None: ...

    def stop(self) -> None: ...


class ExchangeAdapter(ABC):
    """Async interface implemented by every exchange integration.

    Instances are async context managers and own their HTTP connection pool; always ``close()`` them.
    """

    #: registry id, e.g. ``"binance_usdm"``
    exchange_id: ClassVar[str] = ""
    #: human readable name
    display_name: ClassVar[str] = ""
    #: experimental adapters are disabled unless ``EXPERIMENTAL_EXCHANGES_ENABLED=true``
    experimental: ClassVar[bool] = False
    capabilities: ClassVar[ExchangeCapabilities] = ExchangeCapabilities()

    testnet: bool

    async def __aenter__(self) -> ExchangeAdapter:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @abstractmethod
    async def close(self) -> None: ...

    # ---------------------------------------------------------------- market data (public REST)
    @abstractmethod
    async def ping(self) -> Any: ...

    @abstractmethod
    async def server_time(self) -> int:
        """Exchange time in epoch milliseconds."""

    @abstractmethod
    async def sync_time(self) -> int:
        """Store and return the local clock offset (ms) used for signed requests."""

    @abstractmethod
    async def get_exchange_info(self) -> dict: ...

    @abstractmethod
    async def get_symbol_filters(self, symbol: str) -> SymbolFilters:
        """Tick size, step size and minimums (precision) for ``symbol``; cached by the adapter."""

    @abstractmethod
    async def get_klines(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 500,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[list[Any]]: ...

    @abstractmethod
    async def get_klines_range(self, symbol: str, interval: str, start_time: int, end_time: int) -> list[list[Any]]:
        """All klines between two timestamps (ms), paginating as needed (oldest first)."""

    @abstractmethod
    async def get_mark_price(self, symbol: str) -> dict: ...

    @abstractmethod
    async def get_24h_tickers(self) -> list[dict]: ...

    @abstractmethod
    async def get_premium_index_all(self) -> list[dict]:
        """Mark price, index price and current funding rate of every perpetual."""

    @abstractmethod
    async def get_ticker_price(self, symbol: str) -> Decimal: ...

    @abstractmethod
    async def get_funding_rate_history(
        self, symbol: str, start_time: int | None = None, end_time: int | None = None, limit: int = 100
    ) -> list[dict]:
        """Historical funding: ``[{"symbol", "fundingRate", "fundingTime"}]`` (oldest first)."""

    # ---------------------------------------------------------------- account (signed)
    @abstractmethod
    async def get_account(self) -> dict: ...

    @abstractmethod
    async def get_balance(self) -> list[dict]: ...

    @abstractmethod
    async def get_positions(self, symbol: str | None = None, include_flat: bool = False) -> list[dict]: ...

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> dict: ...

    @abstractmethod
    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> dict:
        """Idempotent: "already set" must not raise."""

    @abstractmethod
    async def get_position_mode(self) -> bool:
        """True if hedge mode (two-sided positions) is enabled."""

    @abstractmethod
    async def get_realized_pnl(self, start_time: int, end_time: int | None = None) -> Decimal:
        """Realized PnL + commissions + funding since ``start_time`` (ms)."""

    async def get_leverage_brackets(self, symbol: str) -> list[dict]:
        """Notional brackets of ``symbol``: ``[{"notionalFloor", "notionalCap", "maintMarginRatio", "cum",
        "initialLeverage"}]`` (ascending). Optional: services fall back to a default maintenance margin."""
        raise UnsupportedFeatureError("leverage brackets", self.exchange_id)

    async def get_transfers(self, start_time: int, end_time: int | None = None) -> list[dict]:
        """Deposits/withdrawals of the futures wallet since ``start_time`` (ms):
        ``[{"time" (ms), "amount" (signed: + deposit, - withdrawal), "asset", "type", "tranId"}]``.
        Optional: risk baselines are not adjusted for transfers when unsupported."""
        raise UnsupportedFeatureError("transfer history", self.exchange_id)

    @abstractmethod
    async def get_user_trades(
        self, symbol: str, start_time: int | None = None, from_id: int | None = None, limit: int = 500
    ) -> list[dict]: ...

    # ---------------------------------------------------------------- orders (signed)
    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal | str | None = None,
        price: Decimal | str | None = None,
        stop_price: Decimal | str | None = None,
        time_in_force: str | None = None,
        reduce_only: bool | None = None,
        close_position: bool | None = None,
        client_order_id: str | None = None,
        **extra: Any,
    ) -> dict:
        """Place an order; ``client_order_id`` must be forwarded so retries are idempotent."""

    @abstractmethod
    async def cancel_order(
        self, symbol: str, order_id: int | str | None = None, client_order_id: str | None = None
    ) -> dict: ...

    @abstractmethod
    async def cancel_all_orders(self, symbol: str) -> dict: ...

    @abstractmethod
    async def get_order(
        self, symbol: str, order_id: int | str | None = None, client_order_id: str | None = None
    ) -> dict: ...

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[dict]: ...

    # ---------------------------------------------------------------- streams
    @abstractmethod
    def user_data_stream(self, handler: StreamHandler, **kw: Any) -> ExchangeStream:
        """Private stream emitting canonical ``ORDER_TRADE_UPDATE`` / ``ACCOUNT_UPDATE`` events."""

    @classmethod
    @abstractmethod
    def market_stream(
        cls, streams: list[str], handler: StreamHandler, *, testnet: bool = True, **kw: Any
    ) -> ExchangeStream:
        """Public combined stream (stream names in canonical ``<symbol>@markPrice@1s`` form)."""
