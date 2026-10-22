"""Bybit V5 linear (USDT perpetual) adapter — EXPERIMENTAL SKELETON, disabled by default.

Status:
* REST market data, symbol filters, funding, account, positions and orders are implemented and translated
  into the canonical (Binance-shaped) payloads documented in ``app.exchanges.base``;
* WebSocket streams (public and private, ``app.exchanges.bybit_ws``) are implemented but **off by default**:
  with ``BYBIT_WS_ENABLED=false`` both methods raise ``UnsupportedFeatureError`` and Bybit accounts rely on
  the periodic REST reconciliation — use it for development on the **testnet** only;
* only one-way mode (``positionIdx=0``) on a Unified Trading Account (``accountType=UNIFIED``) is supported.

The registry refuses to build this adapter unless ``EXPERIMENTAL_EXCHANGES_ENABLED=true``.

Docs: https://bybit-exchange.github.io/docs/v5/intro
Auth: HMAC-SHA256 of ``timestamp + api_key + recv_window + (query string | JSON body)``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.exchanges.base import (
    ERR_CANCEL_REJECTED,
    ERR_INVALID_SYMBOL,
    ERR_NO_CHANGE,
    ERR_ORDER_NOT_FOUND,
    ExchangeAdapter,
    ExchangeAPIError,
    ExchangeCapabilities,
    StreamHandler,
    UnsupportedFeatureError,
)
from app.services.symbol_filters import SymbolFilters

logger = logging.getLogger(__name__)

CATEGORY = "linear"

# Binance interval -> Bybit interval
INTERVALS = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "2h": "120", "4h": "240",
    "6h": "360", "12h": "720", "1d": "D", "1w": "W", "1M": "M",
}  # fmt: skip
INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
    "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000, "12h": 43_200_000, "1d": 86_400_000,
    "1w": 604_800_000, "1M": 2_592_000_000,
}  # fmt: skip
KLINE_PAGE = 1000

ORDER_STATUS = {
    "Created": "NEW", "New": "NEW", "Untriggered": "NEW", "Triggered": "NEW", "Active": "NEW",
    "PartiallyFilled": "PARTIALLY_FILLED", "Filled": "FILLED", "Cancelled": "CANCELED",
    "PartiallyFilledCanceled": "CANCELED", "Deactivated": "CANCELED", "Rejected": "REJECTED",
}  # fmt: skip
TIME_IN_FORCE = {"GTC": "GTC", "IOC": "IOC", "FOK": "FOK", "GTX": "PostOnly"}
TRIGGER_BY = {"MARK_PRICE": "MarkPrice", "CONTRACT_PRICE": "LastPrice"}
# Binance-only extras that have no Bybit equivalent and are safe to drop.
IGNORED_EXTRAS = {"newOrderRespType", "priceProtect"}

# Bybit retCode -> canonical code
RET_CODES = {
    10001: -1102,  # parameter error
    10003: -2015,  # invalid api key
    10004: -1022,  # invalid signature
    10002: -1021,  # timestamp outside recvWindow
    110001: ERR_ORDER_NOT_FOUND,
    110043: ERR_NO_CHANGE,  # leverage not modified
    110026: ERR_NO_CHANGE,  # cross/isolated margin mode not modified
    110025: ERR_NO_CHANGE,  # position mode not modified
}

_instruments_cache: dict[str, tuple[float, dict[str, dict]]] = {}
INSTRUMENTS_TTL = 3600.0


def _fmt(value: Decimal | float | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal("0")
    except ArithmeticError:
        return Decimal("0")


class BybitAPIError(ExchangeAPIError):
    def __init__(self, status_code: int, ret_code: int | None, message: str):
        super().__init__(status_code, RET_CODES.get(ret_code, ret_code) if ret_code is not None else None, message)
        self.ret_code = ret_code


def kline_to_canonical(row: list[Any], interval: str) -> list[Any]:
    """[start, open, high, low, close, volume, turnover] -> Binance kline row."""
    start = int(row[0])
    return [start, row[1], row[2], row[3], row[4], row[5], start + INTERVAL_MS[interval] - 1, row[6], 0, "0", "0", "0"]


def instrument_to_symbol_info(item: dict[str, Any]) -> dict[str, Any]:
    price = item.get("priceFilter") or {}
    lot = item.get("lotSizeFilter") or {}
    filters = [
        {"filterType": "PRICE_FILTER", "tickSize": price.get("tickSize", "0.01")},
        {
            "filterType": "LOT_SIZE",
            "stepSize": lot.get("qtyStep", "0.001"),
            "minQty": lot.get("minOrderQty", "0.001"),
            "maxQty": lot.get("maxOrderQty", "1000000"),
        },
        {"filterType": "MIN_NOTIONAL", "notional": lot.get("minNotionalValue", "5")},
    ]
    if lot.get("maxMktOrderQty"):
        filters.append(
            {"filterType": "MARKET_LOT_SIZE", "stepSize": lot.get("qtyStep"), "maxQty": lot["maxMktOrderQty"]}
        )
    return {
        "symbol": item["symbol"],
        "status": "TRADING" if item.get("status") == "Trading" else str(item.get("status", "")).upper(),
        "contractType": "PERPETUAL" if item.get("contractType") == "LinearPerpetual" else item.get("contractType"),
        "baseAsset": item.get("baseCoin"),
        "quoteAsset": item.get("quoteCoin"),
        "marginAsset": item.get("settleCoin"),
        "filters": filters,
    }


def order_to_canonical(o: dict[str, Any]) -> dict[str, Any]:
    order_type = str(o.get("orderType", "")).upper()
    if o.get("triggerPrice") not in (None, "", "0"):
        order_type = "STOP_MARKET" if order_type == "MARKET" else "STOP"
    return {
        "orderId": o.get("orderId"),
        "clientOrderId": o.get("orderLinkId"),
        "symbol": o.get("symbol"),
        "status": ORDER_STATUS.get(o.get("orderStatus", ""), "NEW"),
        "side": str(o.get("side", "")).upper(),
        "type": order_type,
        "origQty": o.get("qty", "0"),
        "executedQty": o.get("cumExecQty", "0"),
        "avgPrice": o.get("avgPrice") or "0",
        "price": o.get("price", "0"),
        "stopPrice": o.get("triggerPrice", "0"),
        "reduceOnly": bool(o.get("reduceOnly")),
        "updateTime": int(o.get("updatedTime") or 0),
    }


def position_to_canonical(p: dict[str, Any]) -> dict[str, Any]:
    size = _dec(p.get("size"))
    signed = -size if p.get("side") == "Sell" else size
    return {
        "symbol": p.get("symbol"),
        "positionAmt": format(signed.normalize(), "f") if signed else "0",
        "entryPrice": p.get("avgPrice") or "0",
        "markPrice": p.get("markPrice") or "0",
        "unRealizedProfit": p.get("unrealisedPnl") or "0",
        "liquidationPrice": p.get("liqPrice") or "0",
        "leverage": p.get("leverage") or "1",
        "positionSide": "BOTH",
        "marginType": "isolated" if str(p.get("tradeMode")) == "1" else "cross",
    }


class BybitLinearAdapter(ExchangeAdapter):
    exchange_id = "bybit_linear"
    display_name = "Bybit USDT Perpetual (experimental)"
    experimental = True
    capabilities = ExchangeCapabilities(
        perpetual_futures=True, testnet=True, hedge_mode=False, user_data_stream=False, market_streams=False,
        test_order=False, quote_assets=("USDT",),
        notes=(
            "experimental: WebSocket streams only with BYBIT_WS_ENABLED=true (otherwise REST polling)",
            "one-way mode, Unified Trading Account only",
        ),
    )  # fmt: skip

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
        self.testnet = True if testnet is None else testnet
        self.base_url = base_url or (settings.BYBIT_TESTNET_API_URL if self.testnet else settings.BYBIT_API_URL)
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self.recv_window = recv_window or settings.BYBIT_RECV_WINDOW
        self.time_offset_ms = 0
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    def sign(self, timestamp: int, payload: str) -> str:
        msg = f"{timestamp}{self._api_key}{self.recv_window}{payload}"
        return hmac.new(self._api_secret, msg.encode("utf-8"), hashlib.sha256).hexdigest()

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None, signed: bool = False) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers: dict[str, str] = {}
        if method == "GET":
            payload = urlencode(params)
            url, content = (f"{path}?{payload}" if payload else path), None
        else:
            payload = json.dumps(params, separators=(",", ":"))
            url, content = path, payload
            headers["Content-Type"] = "application/json"
        if signed:
            if not self._api_key:
                raise BybitAPIError(401, None, "API key required")
            ts = int(time.time() * 1000) + self.time_offset_ms
            headers.update({
                "X-BAPI-API-KEY": self._api_key,
                "X-BAPI-TIMESTAMP": str(ts),
                "X-BAPI-RECV-WINDOW": str(self.recv_window),
                "X-BAPI-SIGN": self.sign(ts, payload),
                "X-BAPI-SIGN-TYPE": "2",
            })  # fmt: skip
        resp = await self._client.request(method, url, content=content, headers=headers)
        try:
            body = resp.json()
        except ValueError:
            raise BybitAPIError(resp.status_code, None, resp.text) from None
        if resp.status_code >= 400 or body.get("retCode", 0) != 0:
            raise BybitAPIError(
                resp.status_code if resp.status_code >= 400 else 400, body.get("retCode"), body.get("retMsg", resp.text)
            )
        return body.get("result") or {}

    # ------------------------------------------------------------------ #
    # Market data (public)
    async def ping(self) -> dict:
        await self.server_time()
        return {}

    async def server_time(self) -> int:
        data = await self._request("GET", "/v5/market/time")
        return int(data["timeNano"]) // 1_000_000 if data.get("timeNano") else int(data["timeSecond"]) * 1000

    async def sync_time(self) -> int:
        self.time_offset_ms = await self.server_time() - int(time.time() * 1000)
        return self.time_offset_ms

    async def _instruments(self) -> dict[str, dict]:
        cached = _instruments_cache.get(self.base_url)
        if cached is None or time.monotonic() - cached[0] > INSTRUMENTS_TTL:
            items: dict[str, dict] = {}
            cursor: str | None = None
            for _ in range(20):  # safety bound on pagination
                data = await self._request(
                    "GET", "/v5/market/instruments-info", {"category": CATEGORY, "limit": 1000, "cursor": cursor}
                )
                for item in data.get("list", []):
                    items[item["symbol"]] = instrument_to_symbol_info(item)
                cursor = data.get("nextPageCursor") or None
                if not cursor:
                    break
            cached = (time.monotonic(), items)
            _instruments_cache[self.base_url] = cached
        return cached[1]

    async def get_exchange_info(self) -> dict:
        return {"symbols": list((await self._instruments()).values())}

    async def get_symbol_filters(self, symbol: str) -> SymbolFilters:
        info = (await self._instruments()).get(symbol)
        if info is None:
            raise BybitAPIError(400, ERR_INVALID_SYMBOL, f"Invalid symbol {symbol}")
        return SymbolFilters.from_exchange_info(info)

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 500,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[list[Any]]:
        if interval not in INTERVALS:
            raise BybitAPIError(400, 10001, f"unsupported interval {interval}")
        data = await self._request(
            "GET",
            "/v5/market/kline",
            {
                "category": CATEGORY, "symbol": symbol, "interval": INTERVALS[interval], "start": start_time,
                "end": end_time, "limit": min(limit, KLINE_PAGE),
            },
        )  # fmt: skip
        rows = [kline_to_canonical(r, interval) for r in data.get("list", [])]
        rows.sort(key=lambda r: r[0])  # Bybit returns newest first
        return rows

    async def get_klines_range(self, symbol: str, interval: str, start_time: int, end_time: int) -> list[list[Any]]:
        rows: list[list[Any]] = []
        cursor = start_time
        while cursor < end_time:
            batch = await self.get_klines(symbol, interval, limit=KLINE_PAGE, start_time=cursor, end_time=end_time)
            batch = [r for r in batch if r[0] >= cursor]
            if not batch:
                break
            rows.extend(batch)
            next_cursor = int(batch[-1][6]) + 1
            if next_cursor <= cursor or len(batch) < KLINE_PAGE:
                break
            cursor = next_cursor
        return rows

    async def _tickers(self, symbol: str | None = None) -> list[dict]:
        return (await self._request("GET", "/v5/market/tickers", {"category": CATEGORY, "symbol": symbol})).get(
            "list", []
        )

    @staticmethod
    def _premium(t: dict[str, Any]) -> dict[str, Any]:
        return {
            "symbol": t.get("symbol"),
            "markPrice": t.get("markPrice"),
            "indexPrice": t.get("indexPrice"),
            "lastFundingRate": t.get("fundingRate") or "0",
            "nextFundingTime": int(t.get("nextFundingTime") or 0),
        }

    async def get_mark_price(self, symbol: str) -> dict:
        tickers = await self._tickers(symbol)
        if not tickers:
            raise BybitAPIError(400, ERR_INVALID_SYMBOL, f"Invalid symbol {symbol}")
        return self._premium(tickers[0])

    async def get_24h_tickers(self) -> list[dict]:
        out = []
        for t in await self._tickers():
            pct = _dec(t.get("price24hPcnt")) * 100
            out.append({
                "symbol": t.get("symbol"),
                "lastPrice": t.get("lastPrice"),
                "priceChangePercent": format(pct.normalize(), "f"),
                "highPrice": t.get("highPrice24h"),
                "lowPrice": t.get("lowPrice24h"),
                "volume": t.get("volume24h"),
                "quoteVolume": t.get("turnover24h"),
            })  # fmt: skip
        return out

    async def get_premium_index_all(self) -> list[dict]:
        return [self._premium(t) for t in await self._tickers()]

    async def get_ticker_price(self, symbol: str) -> Decimal:
        tickers = await self._tickers(symbol)
        if not tickers:
            raise BybitAPIError(400, ERR_INVALID_SYMBOL, f"Invalid symbol {symbol}")
        return Decimal(str(tickers[0]["lastPrice"]))

    async def get_funding_rate_history(
        self, symbol: str, start_time: int | None = None, end_time: int | None = None, limit: int = 100
    ) -> list[dict]:
        data = await self._request(
            "GET",
            "/v5/market/funding/history",
            {
                "category": CATEGORY,
                "symbol": symbol,
                "startTime": start_time,
                "endTime": end_time,
                "limit": min(limit, 200),
            },
        )
        rows = [
            {
                "symbol": r.get("symbol"),
                "fundingRate": r.get("fundingRate"),
                "fundingTime": int(r.get("fundingRateTimestamp") or 0),
            }
            for r in data.get("list", [])
        ]
        rows.sort(key=lambda r: r["fundingTime"])
        return rows

    # ------------------------------------------------------------------ #
    # Account (signed)
    async def _wallet(self) -> dict[str, Any]:
        data = await self._request(
            "GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"}, signed=True
        )
        accounts = data.get("list") or [{}]
        return accounts[0]

    async def get_balance(self) -> list[dict]:
        wallet = await self._wallet()
        out = []
        for c in wallet.get("coin", []):
            out.append({
                "asset": c.get("coin"),
                "balance": c.get("walletBalance") or "0",
                "crossUnPnl": c.get("unrealisedPnl") or "0",
                "availableBalance": wallet.get("totalAvailableBalance") or c.get("availableToWithdraw") or "0",
            })  # fmt: skip
        return out

    async def get_account(self) -> dict:
        wallet = await self._wallet()
        usdt = next((c for c in wallet.get("coin", []) if c.get("coin") == "USDT"), {})
        return {
            "totalWalletBalance": usdt.get("walletBalance") or "0",
            "totalUnrealizedProfit": usdt.get("unrealisedPnl") or "0",
            "totalMarginBalance": usdt.get("equity") or "0",
            "availableBalance": wallet.get("totalAvailableBalance") or "0",
            "canTrade": True,
            "assets": [{"asset": "USDT", "walletBalance": usdt.get("walletBalance") or "0"}],
            "positions": await self.get_positions(),
        }

    async def get_positions(self, symbol: str | None = None, include_flat: bool = False) -> list[dict]:
        params: dict[str, Any] = {"category": CATEGORY, "limit": 200}
        params.update({"symbol": symbol} if symbol else {"settleCoin": "USDT"})
        data = await self._request("GET", "/v5/position/list", params, signed=True)
        positions = [position_to_canonical(p) for p in data.get("list", [])]
        if include_flat:
            return positions
        return [p for p in positions if _dec(p["positionAmt"]) != 0]

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        try:
            await self._request(
                "POST",
                "/v5/position/set-leverage",
                {"category": CATEGORY, "symbol": symbol, "buyLeverage": str(leverage), "sellLeverage": str(leverage)},
                signed=True,
            )
        except BybitAPIError as exc:
            if exc.code != ERR_NO_CHANGE:
                raise
        return {"symbol": symbol, "leverage": leverage}

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> dict:
        """Unified accounts set the margin mode per account (``ISOLATED_MARGIN`` / ``REGULAR_MARGIN``)."""
        mode = "ISOLATED_MARGIN" if margin_type.upper() == "ISOLATED" else "REGULAR_MARGIN"
        try:
            await self._request("POST", "/v5/account/set-margin-mode", {"setMarginMode": mode}, signed=True)
        except BybitAPIError as exc:
            if exc.code == ERR_NO_CHANGE:
                return {"code": exc.code, "msg": exc.message}
            raise
        return {"code": 200, "msg": "success"}

    async def get_position_mode(self) -> bool:
        positions = await self.get_positions(include_flat=True)
        return any(int(p.get("positionIdx", 0) or 0) != 0 for p in positions)

    async def get_realized_pnl(self, start_time: int, end_time: int | None = None) -> Decimal:
        """Sum of closed PnL (already net of fees) since ``start_time``. Funding is not included yet."""
        total = Decimal("0")
        cursor: str | None = None
        for _ in range(20):
            data = await self._request(
                "GET",
                "/v5/position/closed-pnl",
                {"category": CATEGORY, "startTime": start_time, "endTime": end_time, "limit": 100, "cursor": cursor},
                signed=True,
            )
            for row in data.get("list", []):
                total += _dec(row.get("closedPnl"))
            cursor = data.get("nextPageCursor") or None
            if not cursor:
                break
        return total

    async def get_user_trades(
        self, symbol: str, start_time: int | None = None, from_id: int | None = None, limit: int = 500
    ) -> list[dict]:
        data = await self._request(
            "GET",
            "/v5/execution/list",
            {"category": CATEGORY, "symbol": symbol, "startTime": start_time, "limit": min(limit, 100)},
            signed=True,
        )
        return [
            {
                "symbol": e.get("symbol"), "id": e.get("execId"), "orderId": e.get("orderId"),
                "side": str(e.get("side", "")).upper(), "price": e.get("execPrice"), "qty": e.get("execQty"),
                "commission": e.get("execFee"), "commissionAsset": "USDT", "realizedPnl": "0",
                "time": int(e.get("execTime") or 0),
            }
            for e in data.get("list", [])
        ]  # fmt: skip

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
        order_type = order_type.upper()
        side = side.upper()
        if order_type not in ("MARKET", "LIMIT", "STOP_MARKET", "TAKE_PROFIT_MARKET"):
            raise UnsupportedFeatureError(f"order type {order_type}", self.exchange_id)
        unknown = set(extra) - IGNORED_EXTRAS - {"workingType"}
        if unknown:
            raise BybitAPIError(400, 10001, f"unsupported order parameters: {sorted(unknown)}")
        body: dict[str, Any] = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": "Buy" if side == "BUY" else "Sell",
            "orderType": "Limit" if order_type == "LIMIT" else "Market",
            "qty": _fmt(quantity) if quantity is not None else ("0" if close_position else None),
            "price": _fmt(price) if order_type == "LIMIT" else None,
            "timeInForce": TIME_IN_FORCE.get(time_in_force or "", None) if order_type == "LIMIT" else None,
            "orderLinkId": client_order_id,
            "positionIdx": 0,
            "reduceOnly": True if (reduce_only or close_position) else None,
        }
        if order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            if stop_price is None:
                raise BybitAPIError(400, 10001, "stop_price is required")
            rising = (order_type == "STOP_MARKET") == (side == "BUY")
            body.update({
                "triggerPrice": _fmt(stop_price),
                "triggerDirection": 1 if rising else 2,
                "triggerBy": TRIGGER_BY.get(extra.get("workingType", "CONTRACT_PRICE"), "LastPrice"),
                "closeOnTrigger": True if close_position else None,
            })  # fmt: skip
        if body["qty"] is None:
            raise BybitAPIError(400, 10001, "quantity is required")
        data = await self._request("POST", "/v5/order/create", body, signed=True)
        return {
            "orderId": data.get("orderId"),
            "clientOrderId": data.get("orderLinkId", client_order_id),
            "symbol": symbol,
            "status": "NEW",  # Bybit acks asynchronously; fills arrive via get_order / streams
            "side": side,
            "type": order_type,
            "origQty": body["qty"],
            "executedQty": "0",
            "avgPrice": "0",
            "price": body.get("price") or "0",
        }

    async def cancel_order(
        self, symbol: str, order_id: int | str | None = None, client_order_id: str | None = None
    ) -> dict:
        if order_id is None and client_order_id is None:
            raise ValueError("order_id or client_order_id is required")
        try:
            data = await self._request(
                "POST",
                "/v5/order/cancel",
                {
                    "category": CATEGORY,
                    "symbol": symbol,
                    "orderId": None if order_id is None else str(order_id),
                    "orderLinkId": client_order_id,
                },
                signed=True,
            )
        except BybitAPIError as exc:
            if exc.code == ERR_ORDER_NOT_FOUND:
                exc.code = ERR_CANCEL_REJECTED  # Binance: -2011 "Unknown order sent"
            raise
        return {
            "orderId": data.get("orderId"),
            "clientOrderId": data.get("orderLinkId"),
            "symbol": symbol,
            "status": "CANCELED",
        }

    async def cancel_all_orders(self, symbol: str) -> dict:
        await self._request("POST", "/v5/order/cancel-all", {"category": CATEGORY, "symbol": symbol}, signed=True)
        return {"code": 200, "msg": "The operation of cancel all open order is done."}

    async def get_order(
        self, symbol: str, order_id: int | str | None = None, client_order_id: str | None = None
    ) -> dict:
        params = {
            "category": CATEGORY,
            "symbol": symbol,
            "orderId": None if order_id is None else str(order_id),
            "orderLinkId": client_order_id,
        }
        for path in ("/v5/order/realtime", "/v5/order/history"):
            rows = (await self._request("GET", path, params, signed=True)).get("list", [])
            if rows:
                return order_to_canonical(rows[0])
        raise BybitAPIError(400, 110001, "Order does not exist.")

    async def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"category": CATEGORY, "openOnly": 0, "limit": 50}
        params.update({"symbol": symbol} if symbol else {"settleCoin": "USDT"})
        data = await self._request("GET", "/v5/order/realtime", params, signed=True)
        return [order_to_canonical(o) for o in data.get("list", [])]

    # ------------------------------------------------------------------ #
    # Streams (experimental, BYBIT_WS_ENABLED): canonical events, see app.exchanges.bybit_ws
    def user_data_stream(self, handler: StreamHandler, **kw: Any):
        from app.exchanges.bybit_ws import BybitUserDataStream, require_enabled

        require_enabled("user data stream")
        return BybitUserDataStream(
            self._api_key, self._api_secret.decode("utf-8"), handler, testnet=self.testnet, client=self, **kw
        )

    @classmethod
    def market_stream(cls, streams: list[str], handler: StreamHandler, *, testnet: bool = True, **kw: Any):
        from app.exchanges.bybit_ws import BybitMarketStream, require_enabled

        require_enabled("market stream")
        return BybitMarketStream(streams, handler, testnet=testnet, **kw)


__all__ = ["BybitAPIError", "BybitLinearAdapter"]
