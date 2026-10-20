"""Async wrapper around the Binance USDT-M Futures REST API (/fapi)."""
from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import settings


class BinanceAPIError(Exception):
    def __init__(self, status_code: int, code: int | None, message: str):
        super().__init__(f"[{status_code}] {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


class BinanceFuturesClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool | None = None,
        recv_window: int | None = None,
        timeout: float = 10.0,
        base_url: str | None = None,
    ) -> None:
        self.testnet = settings.BINANCE_USE_TESTNET if testnet is None else testnet
        self.base_url = base_url or (settings.BINANCE_TESTNET_API_URL if self.testnet else settings.BINANCE_API_URL)
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self.recv_window = recv_window or settings.BINANCE_RECV_WINDOW
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, headers={"X-MBX-APIKEY": api_key}
        )

    async def __aenter__(self) -> "BinanceFuturesClient":
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
        self, method: str, path: str, params: dict[str, Any] | None = None, signed: bool = False
    ) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = self.recv_window
            params["signature"] = self.sign(params)
        resp = await self._client.request(method, path, params=params)
        if resp.status_code >= 400:
            try:
                body = resp.json()
                raise BinanceAPIError(resp.status_code, body.get("code"), body.get("msg", resp.text))
            except ValueError:
                raise BinanceAPIError(resp.status_code, None, resp.text) from None
        return resp.json()

    # ------------------------------------------------------------------ #
    # Market data (public)
    async def ping(self) -> dict:
        return await self._request("GET", "/fapi/v1/ping")

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

    # Account (signed)
    async def get_account(self) -> dict:
        return await self._request("GET", "/fapi/v2/account", signed=True)

    async def get_positions(self, symbol: str | None = None) -> list[dict]:
        positions = await self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        return [p for p in positions if Decimal(p.get("positionAmt", "0")) != 0]

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        return await self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

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
        client_order_id: str | None = None,
        **extra: Any,
    ) -> dict:
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": str(quantity) if quantity is not None else None,
            "price": str(price) if price is not None else None,
            "stopPrice": str(stop_price) if stop_price is not None else None,
            "timeInForce": time_in_force,
            "reduceOnly": ("true" if reduce_only else "false") if reduce_only is not None else None,
            "newClientOrderId": client_order_id,
            **extra,
        }
        return await self._request("POST", "/fapi/v1/order", params, signed=True)

    async def cancel_order(
        self, symbol: str, order_id: int | None = None, client_order_id: str | None = None
    ) -> dict:
        if order_id is None and client_order_id is None:
            raise ValueError("order_id or client_order_id is required")
        return await self._request(
            "DELETE",
            "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id, "origClientOrderId": client_order_id},
            signed=True,
        )

    async def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        return await self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)
