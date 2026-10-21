"""Async wrapper around the Binance USDT-M Futures REST API (/fapi).

Docs: https://developers.binance.com/docs/derivatives/usds-margined-futures
Signed endpoints use HMAC-SHA256 over the urlencoded query string.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.services.symbol_filters import SymbolFilters


class BinanceAPIError(Exception):
    def __init__(self, status_code: int, code: int | None, message: str):
        super().__init__(f"[{status_code}] {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


# Error codes that mean "nothing to change" for idempotent setters.
NO_CHANGE_CODES = {-4046, -4059}  # margin type / position side unchanged

_exchange_info_cache: dict[str, tuple[float, dict[str, SymbolFilters]]] = {}
EXCHANGE_INFO_TTL = 3600.0


def _fmt(value: Decimal | float | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


class BinanceFuturesClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool | None = None,
        recv_window: int | None = None,
        timeout: float = 10.0,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.testnet = settings.BINANCE_USE_TESTNET if testnet is None else testnet
        self.base_url = base_url or (settings.BINANCE_TESTNET_API_URL if self.testnet else settings.BINANCE_API_URL)
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self.recv_window = recv_window or settings.BINANCE_RECV_WINDOW
        self.time_offset_ms = 0
        headers = {"X-MBX-APIKEY": api_key} if api_key else {}
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, headers=headers, transport=transport)

    async def __aenter__(self) -> BinanceFuturesClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

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
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
            params["recvWindow"] = self.recv_window
            params["signature"] = self.sign(params)
        resp = await self._client.request(method, path, params=params)
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
        return await self._request("GET", "/fapi/v1/exchangeInfo")

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
        return await self._request("POST", "/fapi/v1/order", params, signed=True)

    async def test_order(self, symbol: str, side: str, order_type: str, **kwargs: Any) -> dict:
        """Validates an order without sending it to the matching engine (/fapi/v1/order/test)."""
        params = {"symbol": symbol, "side": side, "type": order_type, **{k: _fmt(v) for k, v in kwargs.items()}}
        return await self._request("POST", "/fapi/v1/order/test", params, signed=True)

    async def cancel_order(self, symbol: str, order_id: int | None = None, client_order_id: str | None = None) -> dict:
        if order_id is None and client_order_id is None:
            raise ValueError("order_id or client_order_id is required")
        return await self._request(
            "DELETE",
            "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id, "origClientOrderId": client_order_id},
            signed=True,
        )

    async def cancel_all_orders(self, symbol: str) -> dict:
        return await self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)

    async def get_order(self, symbol: str, order_id: int | None = None, client_order_id: str | None = None) -> dict:
        return await self._request(
            "GET",
            "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id, "origClientOrderId": client_order_id},
            signed=True,
        )

    async def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        return await self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)

    # ------------------------------------------------------------------ #
    # User data stream (API key header only, no signature)
    async def create_listen_key(self) -> str:
        return (await self._request("POST", "/fapi/v1/listenKey", api_key_only=True))["listenKey"]

    async def keepalive_listen_key(self) -> dict:
        return await self._request("PUT", "/fapi/v1/listenKey", api_key_only=True)

    async def close_listen_key(self) -> dict:
        return await self._request("DELETE", "/fapi/v1/listenKey", api_key_only=True)
