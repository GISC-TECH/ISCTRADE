"""Async wrapper around the Binance USDT-M Futures REST API (/fapi).

Docs: https://developers.binance.com/docs/derivatives/usds-margined-futures
Signed endpoints use HMAC-SHA256 over the urlencoded query string.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.exchanges.base import (
    ERR_CANCEL_REJECTED,
    ERR_ORDER_NOT_FOUND,
    ExchangeAdapter,
    ExchangeAPIError,
    ExchangeCapabilities,
    StreamHandler,
)
from app.services.binance_http import DEFAULT_TIMEOUT, rate_limits, shared_client
from app.services.symbol_filters import SymbolFilters


class BinanceAPIError(ExchangeAPIError):
    """Binance error. Its codes *are* the canonical codes (see ``app.exchanges.base``)."""


# Error codes that mean "nothing to change" for idempotent setters.
NO_CHANGE_CODES = {-4046, -4059}  # margin type / position side unchanged

_exchange_info_cache: dict[str, tuple[float, dict[str, SymbolFilters]]] = {}
EXCHANGE_INFO_TTL = 3600.0
# Raw /fapi/v1/exchangeInfo (~1 MB JSON for USDT-M) is shared by the scanner and symbol filters; listings change
# rarely, so one download per 15 min per process is enough. Only for pooled clients (mocked transports bypass it).
EXCHANGE_INFO_RAW_TTL = 900.0
_exchange_info_raw: dict[str, tuple[float, dict]] = {}


def _fmt(value: Decimal | float | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


ERR_TIMESTAMP = -1021  # timestamp outside recvWindow (clock skew)
ERR_UNKNOWN_STATUS = -1007  # "Timeout waiting for response from backend server. Send status unknown"
CONDITIONAL_TYPES = frozenset({"STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"})
GET_RETRIES = 2
GET_RETRY_BACKOFF = 0.25
RECOVERY_DELAYS = (0.5, 1.0, 2.0)
# algoStatus -> order status used by the rest of the system (TRIGGERED/FINISHED = sent to the book as MARKET)
_ALGO_STATUS = {
    "NEW": "NEW", "TRIGGERING": "NEW", "TRIGGERED": "FILLED", "FINISHED": "FILLED",
    "CANCELED": "CANCELED", "EXPIRED": "EXPIRED", "REJECTED": "REJECTED",
}  # fmt: skip


def normalize_algo_order(algo: dict[str, Any]) -> dict[str, Any]:
    """Algo (conditional) order -> the order-shaped dict the executor/reconciler expect."""
    return {
        **algo,
        "orderId": algo.get("algoId"),
        "clientOrderId": algo.get("clientAlgoId"),
        "type": algo.get("orderType") or algo.get("type"),
        "stopPrice": algo.get("triggerPrice"),
        "status": _ALGO_STATUS.get(str(algo.get("algoStatus") or "NEW").upper(), "NEW"),
        "executedQty": algo.get("executedQty", "0"),
        "avgPrice": algo.get("actualPrice") or algo.get("avgPrice") or "0",
        "isAlgo": True,
    }


def _status_unknown(exc: Exception) -> bool:
    """True when the request may or may not have reached the matching engine."""
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, BinanceAPIError):
        return exc.code == ERR_UNKNOWN_STATUS or exc.status_code >= 500
    return False


class BinanceFuturesClient(ExchangeAdapter):
    """Binance USDT-M Futures adapter (registry id ``binance_usdm``)."""

    exchange_id = "binance_usdm"
    display_name = "Binance USDT-M Futures"
    experimental = False
    capabilities = ExchangeCapabilities(
        perpetual_futures=True, testnet=True, hedge_mode=True, user_data_stream=True, market_streams=True,
        test_order=True, quote_assets=("USDT", "USDC"),
    )  # fmt: skip

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool | None = None,
        recv_window: int | None = None,
        timeout: float | httpx.Timeout | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.testnet = settings.BINANCE_USE_TESTNET if testnet is None else testnet
        self.base_url = base_url or (settings.BINANCE_TESTNET_API_URL if self.testnet else settings.BINANCE_API_URL)
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self.recv_window = recv_window or settings.BINANCE_RECV_WINDOW
        self.time_offset_ms = 0
        self._headers = {"X-MBX-APIKEY": api_key} if api_key else {}
        self._timeout = DEFAULT_TIMEOUT if timeout is None else timeout
        # A custom transport (tests) gets a private client; otherwise requests go through the pooled,
        # keep-alive client of the running event loop (no TCP/TLS handshake per client instance).
        self._own_client: httpx.AsyncClient | None = (
            httpx.AsyncClient(base_url=self.base_url, timeout=self._timeout, transport=transport)
            if transport is not None
            else None
        )

    @property
    def _client(self) -> httpx.AsyncClient:
        return self._own_client if self._own_client is not None else shared_client(self.base_url)

    async def __aenter__(self) -> BinanceFuturesClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._own_client is not None:
            await self._own_client.aclose()

    # ------------------------------------------------------------------ #
    def sign(self, params: dict[str, Any]) -> str:
        """HMAC-SHA256 signature of the urlencoded query string."""
        query = urlencode(params, doseq=True)
        return hmac.new(self._api_secret, query.encode("utf-8"), hashlib.sha256).hexdigest()

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
        api_key_only: bool = False,
    ) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if signed or api_key_only:
            if not self._api_key:
                raise BinanceAPIError(401, None, "API key required")
        attempt = 0
        resynced = False
        while True:
            try:
                return await self._send(method, path, dict(params), signed)
            except BinanceAPIError as exc:
                # -1021: local clock outside recvWindow -> resync with the server time and retry once
                if signed and exc.code == ERR_TIMESTAMP and not resynced:
                    resynced = True
                    await self.sync_time()
                    continue
                if method == "GET" and exc.status_code >= 500 and attempt < GET_RETRIES:
                    attempt += 1
                    await asyncio.sleep(GET_RETRY_BACKOFF * attempt)
                    continue
                raise
            except httpx.TransportError:
                # reads are idempotent: retry transient network errors; writes never retry blindly
                if method == "GET" and attempt < GET_RETRIES:
                    attempt += 1
                    await asyncio.sleep(GET_RETRY_BACKOFF * attempt)
                    continue
                raise

    async def _send(self, method: str, path: str, params: dict[str, Any], signed: bool) -> Any:
        await rate_limits.before_request(self.base_url, method, path)  # before signing: waits keep recvWindow valid
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
            params["recvWindow"] = self.recv_window
            params["signature"] = self.sign(params)
        resp = await self._client.request(method, path, params=params, headers=self._headers, timeout=self._timeout)
        rate_limits.record(self.base_url, resp)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                raise BinanceAPIError(resp.status_code, None, resp.text) from None
            raise BinanceAPIError(resp.status_code, body.get("code"), body.get("msg", resp.text))
        return resp.json()

    # ------------------------------------------------------------------ #
    # Market data (public)
    async def ping(self) -> dict:
        return await self._request("GET", "/fapi/v1/ping")

    async def server_time(self) -> int:
        return int((await self._request("GET", "/fapi/v1/time"))["serverTime"])

    async def sync_time(self) -> int:
        """Compute the local clock offset (avoids -1021 timestamp errors)."""
        self.time_offset_ms = await self.server_time() - int(time.time() * 1000)
        return self.time_offset_ms

    async def get_exchange_info(self) -> dict:
        if self._own_client is not None:
            return await self._request("GET", "/fapi/v1/exchangeInfo")
        cached = _exchange_info_raw.get(self.base_url)
        if cached is not None and time.monotonic() - cached[0] <= EXCHANGE_INFO_RAW_TTL:
            return cached[1]
        info = await self._request("GET", "/fapi/v1/exchangeInfo")
        _exchange_info_raw[self.base_url] = (time.monotonic(), info)
        return info

    async def get_symbol_filters(self, symbol: str) -> SymbolFilters:
        cached = _exchange_info_cache.get(self.base_url)
        if cached is None or time.monotonic() - cached[0] > EXCHANGE_INFO_TTL:
            info = await self.get_exchange_info()
            filters = {s["symbol"]: SymbolFilters.from_exchange_info(s) for s in info.get("symbols", [])}
            cached = (time.monotonic(), filters)
            _exchange_info_cache[self.base_url] = cached
        try:
            return cached[1][symbol]
        except KeyError:
            raise BinanceAPIError(400, -1121, f"Invalid symbol {symbol}") from None

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 500,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[list[Any]]:
        return await self._request(
            "GET",
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit, "startTime": start_time, "endTime": end_time},
        )

    async def get_klines_range(self, symbol: str, interval: str, start_time: int, end_time: int) -> list[list[Any]]:
        """Paginate klines between two timestamps (ms)."""
        rows: list[list[Any]] = []
        cursor = start_time
        while cursor < end_time:
            batch = await self.get_klines(symbol, interval, limit=1500, start_time=cursor, end_time=end_time)
            if not batch:
                break
            rows.extend(batch)
            next_cursor = int(batch[-1][6]) + 1
            if next_cursor <= cursor or len(batch) < 1500:
                break
            cursor = next_cursor
        return rows

    async def get_mark_price(self, symbol: str) -> dict:
        return await self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})

    async def get_24h_tickers(self) -> list[dict]:
        """24h rolling statistics for every symbol (weight 40)."""
        return await self._request("GET", "/fapi/v1/ticker/24hr")

    async def get_premium_index_all(self) -> list[dict]:
        """Mark price, index price and last funding rate for every symbol."""
        return await self._request("GET", "/fapi/v1/premiumIndex")

    async def get_ticker_price(self, symbol: str) -> Decimal:
        data = await self._request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})
        return Decimal(str(data["price"]))

    async def get_funding_rate_history(
        self, symbol: str, start_time: int | None = None, end_time: int | None = None, limit: int = 100
    ) -> list[dict]:
        return await self._request(
            "GET",
            "/fapi/v1/fundingRate",
            {"symbol": symbol, "startTime": start_time, "endTime": end_time, "limit": limit},
        )

    # ------------------------------------------------------------------ #
    # Account (signed)
    async def get_account(self) -> dict:
        return await self._request("GET", "/fapi/v2/account", signed=True)

    async def get_balance(self) -> list[dict]:
        return await self._request("GET", "/fapi/v2/balance", signed=True)

    async def get_positions(self, symbol: str | None = None, include_flat: bool = False) -> list[dict]:
        positions = await self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        if include_flat:
            return positions
        return [p for p in positions if Decimal(str(p.get("positionAmt", "0"))) != 0]

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        return await self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> dict:
        try:
            return await self._request(
                "POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type.upper()}, signed=True
            )
        except BinanceAPIError as exc:
            if exc.code in NO_CHANGE_CODES:
                return {"code": exc.code, "msg": exc.message}
            raise

    async def get_position_mode(self) -> bool:
        """True if hedge mode (dual side position) is enabled."""
        data = await self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        return bool(data.get("dualSidePosition"))

    async def get_income_history(
        self,
        income_type: str | None = None,
        symbol: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        return await self._request(
            "GET",
            "/fapi/v1/income",
            {
                "incomeType": income_type,
                "symbol": symbol,
                "startTime": start_time,
                "endTime": end_time,
                "limit": limit,
            },
            signed=True,
        )

    async def get_realized_pnl(self, start_time: int, end_time: int | None = None) -> Decimal:
        """Sum of REALIZED_PNL + COMMISSION + FUNDING_FEE income since `start_time` (ms)."""
        total = Decimal("0")
        for row in await self.get_income_history(start_time=start_time, end_time=end_time):
            if row.get("incomeType") in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
                total += Decimal(str(row.get("income", "0")))
        return total

    async def get_user_trades(
        self, symbol: str, start_time: int | None = None, from_id: int | None = None, limit: int = 500
    ) -> list[dict]:
        return await self._request(
            "GET",
            "/fapi/v1/userTrades",
            {"symbol": symbol, "startTime": start_time, "fromId": from_id, "limit": limit},
            signed=True,
        )

    # ------------------------------------------------------------------ #
    # Orders (signed)
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
        """Place an order. Conditional types go to the Algo Order API (mandatory since 2025-12-09).

        If the outcome is unknown (timeout, 5xx, -1007) the order is looked up by its client id before
        giving up, so the caller never retries (duplicates) an order that actually reached the exchange.
        """
        if order_type.upper() in CONDITIONAL_TYPES:
            send = self._place_algo_order(
                symbol, side, order_type, quantity=quantity, price=price, trigger_price=stop_price,
                time_in_force=time_in_force, reduce_only=reduce_only, close_position=close_position,
                client_algo_id=client_order_id, **extra,
            )  # fmt: skip
        else:
            params: dict[str, Any] = {
                "symbol": symbol,
                "side": side,
                "type": order_type,
                "quantity": _fmt(quantity),
                "price": _fmt(price),
                "stopPrice": _fmt(stop_price),
                "timeInForce": time_in_force,
                "reduceOnly": ("true" if reduce_only else "false") if reduce_only is not None else None,
                "closePosition": "true" if close_position else None,
                "newClientOrderId": client_order_id,
                **extra,
            }
            send = self._request("POST", "/fapi/v1/order", params, signed=True)
        try:
            return await send
        except (httpx.TimeoutException, httpx.TransportError, BinanceAPIError) as exc:
            if not client_order_id or not _status_unknown(exc):
                raise
            found = await self._recover_order(symbol, client_order_id)
            if found is None:
                raise
            return found

    async def _place_algo_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        *,
        quantity: Decimal | str | None,
        price: Decimal | str | None,
        trigger_price: Decimal | str | None,
        time_in_force: str | None,
        reduce_only: bool | None,
        close_position: bool | None,
        client_algo_id: str | None,
        **extra: Any,
    ) -> dict:
        params: dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": None if close_position else _fmt(quantity),
            "price": _fmt(price),
            "triggerPrice": _fmt(trigger_price),
            "timeInForce": time_in_force,
            # reduceOnly cannot be combined with closePosition=true
            "reduceOnly": None if close_position or reduce_only is None else ("true" if reduce_only else "false"),
            "closePosition": "true" if close_position else None,
            "clientAlgoId": client_algo_id,
            **extra,
        }
        return normalize_algo_order(await self._request("POST", "/fapi/v1/algoOrder", params, signed=True))

    async def _recover_order(self, symbol: str, client_order_id: str) -> dict | None:
        for delay in RECOVERY_DELAYS:
            await asyncio.sleep(delay)
            try:
                return await self.get_order(symbol, client_order_id=client_order_id)
            except BinanceAPIError as exc:
                if exc.code != ERR_ORDER_NOT_FOUND and exc.status_code < 500:
                    return None
            except httpx.TransportError:
                continue
        return None

    async def test_order(self, symbol: str, side: str, order_type: str, **kwargs: Any) -> dict:
        """Validates an order without sending it to the matching engine (/fapi/v1/order/test)."""
        params = {"symbol": symbol, "side": side, "type": order_type, **{k: _fmt(v) for k, v in kwargs.items()}}
        return await self._request("POST", "/fapi/v1/order/test", params, signed=True)

    async def cancel_order(self, symbol: str, order_id: int | None = None, client_order_id: str | None = None) -> dict:
        if order_id is None and client_order_id is None:
            raise ValueError("order_id or client_order_id is required")
        try:
            return await self._request(
                "DELETE",
                "/fapi/v1/order",
                {"symbol": symbol, "orderId": order_id, "origClientOrderId": client_order_id},
                signed=True,
            )
        except BinanceAPIError as exc:
            if exc.code not in (ERR_CANCEL_REJECTED, ERR_ORDER_NOT_FOUND):
                raise
            # conditional (STOP/TP) orders live in the Algo service; ids there are algoId/clientAlgoId
            params = {"clientAlgoId": client_order_id} if client_order_id else {"algoId": order_id}
            try:
                return normalize_algo_order(await self._request("DELETE", "/fapi/v1/algoOrder", params, signed=True))
            except BinanceAPIError:
                raise exc from None

    async def cancel_all_orders(self, symbol: str) -> dict:
        resp = await self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)
        try:  # protective STOP/TP orders are algo orders and are not covered by allOpenOrders
            await self._request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol}, signed=True)
        except BinanceAPIError as exc:
            if exc.status_code >= 500 or exc.code in (ERR_TIMESTAMP,):
                raise
        return resp

    async def get_order(self, symbol: str, order_id: int | None = None, client_order_id: str | None = None) -> dict:
        try:
            return await self._request(
                "GET",
                "/fapi/v1/order",
                {"symbol": symbol, "orderId": order_id, "origClientOrderId": client_order_id},
                signed=True,
            )
        except BinanceAPIError as exc:
            if exc.code != ERR_ORDER_NOT_FOUND or not client_order_id:
                raise
            try:
                algo = await self._request("GET", "/fapi/v1/algoOrder", {"clientAlgoId": client_order_id}, signed=True)
            except BinanceAPIError:
                raise exc from None
            if not algo or not algo.get("algoId"):
                raise exc from None
            return normalize_algo_order(algo)

    async def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        orders = await self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)
        algo = await self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)
        rows = algo.get("orders", algo) if isinstance(algo, dict) else algo
        return [*orders, *(normalize_algo_order(a) for a in rows or [])]

    # ------------------------------------------------------------------ #
    # User data stream (API key header only, no signature)
    async def create_listen_key(self) -> str:
        return (await self._request("POST", "/fapi/v1/listenKey", api_key_only=True))["listenKey"]

    async def keepalive_listen_key(self) -> dict:
        return await self._request("PUT", "/fapi/v1/listenKey", api_key_only=True)

    async def close_listen_key(self) -> dict:
        return await self._request("DELETE", "/fapi/v1/listenKey", api_key_only=True)

    # ------------------------------------------------------------------ #
    # Streams (see app.services.binance_ws)
    def user_data_stream(self, handler: StreamHandler, **kw: Any):
        from app.services.binance_ws import UserDataStream

        return UserDataStream(self, handler, **kw)

    @classmethod
    def market_stream(cls, streams: list[str], handler: StreamHandler, *, testnet: bool = True, **kw: Any):
        from app.services.binance_ws import MarketStream

        return MarketStream(streams, handler, testnet=testnet, **kw)
