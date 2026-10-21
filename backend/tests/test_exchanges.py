"""Exchange adapter interface, registry and the experimental Bybit skeleton (HTTP always mocked)."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from app.core.config import settings
from app.exchanges import registry
from app.exchanges.base import (
    ERR_CANCEL_REJECTED,
    ERR_NO_CHANGE,
    ERR_ORDER_NOT_FOUND,
    ExchangeAdapter,
    ExchangeAPIError,
    UnsupportedFeatureError,
)
from app.exchanges.bybit import BybitAPIError, BybitLinearAdapter
from app.schemas.account import AccountCreate
from app.services.binance_client import BinanceAPIError, BinanceFuturesClient
from tests.factories import make_account, make_user_workspace
from tests.test_api_e2e import bearer, register_login


def ok(result: dict) -> httpx.Response:
    return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result, "time": 1})


def bybit(handler, **kw) -> BybitLinearAdapter:
    return BybitLinearAdapter("bybit-key", "bybit-secret", testnet=True, transport=httpx.MockTransport(handler), **kw)


# --------------------------------------------------------------------------- interface & registry
def test_binance_client_implements_adapter() -> None:
    client = BinanceFuturesClient("", "", testnet=True)
    assert isinstance(client, ExchangeAdapter)
    assert client.exchange_id == "binance_usdm" and not client.experimental
    assert issubclass(BinanceAPIError, ExchangeAPIError)
    assert not BinanceFuturesClient.__abstractmethods__ and not BybitLinearAdapter.__abstractmethods__


def test_registry_defaults_and_experimental_gate(monkeypatch) -> None:
    assert registry.get_adapter_class() is BinanceFuturesClient
    assert set(registry.available_exchanges()) == {"binance_usdm"}
    assert {"binance_usdm", "bybit_linear"} <= set(registry.available_exchanges(include_disabled=True))
    with pytest.raises(registry.UnknownExchangeError, match="experimental"):
        registry.get_adapter_class("bybit_linear")
    with pytest.raises(registry.UnknownExchangeError, match="unknown"):
        registry.get_adapter_class("nope")
    pub = registry.public_adapter(testnet=False)
    assert isinstance(pub, BinanceFuturesClient) and pub.base_url == settings.BINANCE_API_URL

    monkeypatch.setattr(settings, "EXPERIMENTAL_EXCHANGES_ENABLED", True)
    assert registry.get_adapter_class("bybit_linear") is BybitLinearAdapter
    adapter = registry.create_adapter("bybit_linear", "k", "s", testnet=True)
    assert isinstance(adapter, BybitLinearAdapter) and adapter.base_url == settings.BYBIT_TESTNET_API_URL


async def test_adapter_for_account_uses_account_exchange(db, monkeypatch) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws, is_testnet=True)
    assert acc.exchange == "binance_usdm"
    async with registry.adapter_for_account(acc) as client:
        assert isinstance(client, BinanceFuturesClient) and client.testnet and client._api_key == "dummy-key-0000"

    acc.exchange = "bybit_linear"
    with pytest.raises(registry.UnknownExchangeError):
        registry.adapter_for_account(acc)
    monkeypatch.setattr(settings, "EXPERIMENTAL_EXCHANGES_ENABLED", True)
    async with registry.adapter_for_account(acc) as client:
        assert isinstance(client, BybitLinearAdapter) and client._api_key == "dummy-key-0000"


def test_account_create_validates_exchange(monkeypatch) -> None:
    base = {"label": "x", "api_key": "placeholder-key-1234", "api_secret": "placeholder-secret-5678"}
    assert AccountCreate(**base).exchange == "binance_usdm"
    with pytest.raises(ValueError):
        AccountCreate(**base, exchange="bybit_linear")
    with pytest.raises(ValueError):
        AccountCreate(**base, exchange="ftx")
    monkeypatch.setattr(settings, "EXPERIMENTAL_EXCHANGES_ENABLED", True)
    assert AccountCreate(**base, exchange="bybit_linear").exchange == "bybit_linear"


async def test_accounts_api_exchange_field(client) -> None:
    h = bearer(await register_login(client, "exch@example.com"))
    r = await client.get("/api/v1/accounts/exchanges", headers=h)
    assert r.status_code == 200 and [e["id"] for e in r.json()] == ["binance_usdm"]
    body = {"label": "B", "api_key": "placeholder-key-1234", "api_secret": "placeholder-secret-5678"}
    r = await client.post("/api/v1/accounts/", json=body, headers=h)
    assert r.status_code == 201 and r.json()["exchange"] == "binance_usdm"
    r = await client.post("/api/v1/accounts/", json={**body, "exchange": "bybit_linear"}, headers=h)
    assert r.status_code == 422


async def test_binance_funding_rate_history() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"], seen["params"] = request.url.path, dict(request.url.params)
        return httpx.Response(200, json=[{"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingTime": 1}])

    async with BinanceFuturesClient("", "", testnet=True, transport=httpx.MockTransport(handler)) as c:
        rows = await c.get_funding_rate_history("BTCUSDT", limit=10)
    assert seen["path"] == "/fapi/v1/fundingRate" and seen["params"] == {"symbol": "BTCUSDT", "limit": "10"}
    assert rows[0]["fundingRate"] == "0.0001"


# --------------------------------------------------------------------------- Bybit skeleton
async def test_bybit_signing_headers_get_and_post() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/v5/position/list":
            return ok({"list": []})
        return ok({"orderId": "1", "orderLinkId": "cid-1"})

    async with bybit(handler) as c:
        await c.get_positions(symbol="BTCUSDT")
        await c.place_order("BTCUSDT", "BUY", "MARKET", quantity=Decimal("0.010"), client_order_id="cid-1")

    for req in captured:
        ts, recv = req.headers["X-BAPI-TIMESTAMP"], req.headers["X-BAPI-RECV-WINDOW"]
        payload = req.url.query.decode() if req.method == "GET" else req.content.decode()
        expected = hmac.new(b"bybit-secret", f"{ts}bybit-key{recv}{payload}".encode(), hashlib.sha256).hexdigest()
        assert req.headers["X-BAPI-API-KEY"] == "bybit-key" and req.headers["X-BAPI-SIGN"] == expected
    get, post = captured
    assert dict(parse_qsl(urlsplit(str(get.url)).query)) == {"category": "linear", "limit": "200", "symbol": "BTCUSDT"}
    body = json.loads(post.content)
    assert body == {
        "category": "linear", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market", "qty": "0.01",
        "orderLinkId": "cid-1", "positionIdx": 0,
    }  # fmt: skip


async def test_bybit_signed_requires_key() -> None:
    async with BybitLinearAdapter("", "", testnet=True, transport=httpx.MockTransport(lambda r: ok({}))) as c:
        with pytest.raises(BybitAPIError) as exc:
            await c.get_account()
    assert exc.value.status_code == 401


async def test_bybit_market_data_translation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path, q = request.url.path, dict(request.url.params)
        if path == "/v5/market/kline":
            assert q["interval"] == "60" and q["category"] == "linear"
            return ok({"list": [
                ["1700003600000", "101", "103", "100", "102", "5", "510"],
                ["1700000000000", "100", "102", "99", "101", "4", "404"],
            ]})  # fmt: skip
        if path == "/v5/market/instruments-info":
            if q.get("cursor") is None:
                return ok({"nextPageCursor": "p2", "list": [{
                    "symbol": "BTCUSDT", "status": "Trading", "contractType": "LinearPerpetual",
                    "baseCoin": "BTC", "quoteCoin": "USDT", "settleCoin": "USDT",
                    "priceFilter": {"tickSize": "0.10"},
                    "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "500",
                                      "maxMktOrderQty": "100", "minNotionalValue": "5"},
                }]})  # fmt: skip
            return ok({"nextPageCursor": "", "list": [{
                "symbol": "ETHPERP", "status": "Trading", "contractType": "LinearFutures", "quoteCoin": "USDC",
                "priceFilter": {"tickSize": "0.01"}, "lotSizeFilter": {"qtyStep": "0.01"},
            }]})  # fmt: skip
        if path == "/v5/market/tickers":
            return ok({"list": [{
                "symbol": "BTCUSDT", "lastPrice": "65000", "markPrice": "65001", "indexPrice": "64999",
                "fundingRate": "0.0001", "nextFundingTime": "1700028800000", "price24hPcnt": "0.0123",
                "turnover24h": "123456789", "volume24h": "1900", "highPrice24h": "66000", "lowPrice24h": "64000",
            }]})  # fmt: skip
        if path == "/v5/market/funding/history":
            return ok({"list": [
                {"symbol": "BTCUSDT", "fundingRate": "0.0002", "fundingRateTimestamp": "1700028800000"},
                {"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingRateTimestamp": "1700000000000"},
            ]})  # fmt: skip
        if path == "/v5/market/time":
            return ok({"timeSecond": "1700000000", "timeNano": "1700000000123456789"})
        raise AssertionError(path)

    async with bybit(handler, base_url="https://bybit.test") as c:
        rows = await c.get_klines("BTCUSDT", "1h", limit=2)
        assert [r[0] for r in rows] == [1700000000000, 1700003600000]  # oldest first
        assert rows[0][6] == 1700000000000 + 3_600_000 - 1 and rows[0][4] == "101"
        f = await c.get_symbol_filters("BTCUSDT")
        assert (f.tick_size, f.step_size, f.market_max_qty, f.min_notional) == (
            Decimal("0.10"), Decimal("0.001"), Decimal("100"), Decimal("5"),
        )  # fmt: skip
        info = await c.get_exchange_info()
        perps = [s["symbol"] for s in info["symbols"] if s["contractType"] == "PERPETUAL" and s["quoteAsset"] == "USDT"]
        assert perps == ["BTCUSDT"]
        with pytest.raises(BybitAPIError) as exc:
            await c.get_symbol_filters("DOGEUSDT")
        assert exc.value.code == -1121
        t = (await c.get_24h_tickers())[0]
        assert t["quoteVolume"] == "123456789" and t["priceChangePercent"] == "1.23"
        p = (await c.get_premium_index_all())[0]
        assert p["lastFundingRate"] == "0.0001" and p["markPrice"] == "65001"
        assert (await c.get_mark_price("BTCUSDT"))["nextFundingTime"] == 1700028800000
        assert await c.get_ticker_price("BTCUSDT") == Decimal("65000")
        funding = await c.get_funding_rate_history("BTCUSDT")
        assert [x["fundingTime"] for x in funding] == [1700000000000, 1700028800000]
        assert await c.server_time() == 1700000000123
        with pytest.raises(BybitAPIError):
            await c.get_klines("BTCUSDT", "8h")


async def test_bybit_account_and_positions_translation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v5/account/wallet-balance":
            return ok({"list": [{"totalAvailableBalance": "900", "coin": [
                {"coin": "USDT", "walletBalance": "1000", "equity": "1010", "unrealisedPnl": "10"},
            ]}]})  # fmt: skip
        if request.url.path == "/v5/position/list":
            return ok({"list": [
                {"symbol": "BTCUSDT", "side": "Sell", "size": "0.5", "avgPrice": "65000", "markPrice": "64000",
                 "unrealisedPnl": "500", "leverage": "3", "liqPrice": "80000", "tradeMode": 1},
                {"symbol": "ETHUSDT", "side": "", "size": "0", "avgPrice": "0", "leverage": "5"},
            ]})  # fmt: skip
        raise AssertionError(request.url.path)

    async with bybit(handler) as c:
        acc = await c.get_account()
        assert acc["totalWalletBalance"] == "1000" and acc["availableBalance"] == "900"
        assert acc["totalUnrealizedProfit"] == "10" and acc["canTrade"] is True
        assert [p["positionAmt"] for p in acc["positions"]] == ["-0.5"]
        pos = (await c.get_positions(include_flat=True))[0]
        assert pos["marginType"] == "isolated" and pos["entryPrice"] == "65000" and pos["unRealizedProfit"] == "500"
        assert len(await c.get_positions(include_flat=True)) == 2
        assert (await c.get_balance())[0]["balance"] == "1000"


async def test_bybit_order_mapping_and_errors() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST":
            bodies.append(json.loads(request.content))
        if path == "/v5/order/create":
            return ok({"orderId": "abc", "orderLinkId": bodies[-1].get("orderLinkId")})
        if path == "/v5/order/cancel":
            return httpx.Response(200, json={"retCode": 110001, "retMsg": "Order does not exist.", "result": {}})
        if path == "/v5/position/set-leverage":
            return httpx.Response(200, json={"retCode": 110043, "retMsg": "leverage not modified", "result": {}})
        if path == "/v5/order/realtime":
            return ok({"list": []})
        if path == "/v5/order/history":
            q = dict(request.url.params)
            if q.get("orderLinkId") == "missing":
                return ok({"list": []})
            return ok({"list": [{
                "orderId": "abc", "orderLinkId": "cid-2", "symbol": "BTCUSDT", "orderStatus": "Filled",
                "side": "Buy", "orderType": "Market", "qty": "0.01", "cumExecQty": "0.01", "avgPrice": "65000",
                "price": "0", "triggerPrice": "", "updatedTime": "1700000000000",
            }]})  # fmt: skip
        raise AssertionError(path)

    async with bybit(handler) as c:
        resp = await c.place_order(
            "BTCUSDT", "SELL", "STOP_MARKET", stop_price=Decimal("60000.0"), close_position=True,
            client_order_id="cid-sl", workingType="MARK_PRICE",
        )  # fmt: skip
        assert resp["orderId"] == "abc" and resp["clientOrderId"] == "cid-sl" and resp["status"] == "NEW"
        sl = bodies[-1]
        assert sl["triggerPrice"] == "60000" and sl["triggerDirection"] == 2 and sl["triggerBy"] == "MarkPrice"
        assert sl["qty"] == "0" and sl["reduceOnly"] is True and sl["closeOnTrigger"] is True
        assert sl["orderType"] == "Market" and sl["side"] == "Sell"

        await c.place_order("BTCUSDT", "SELL", "TAKE_PROFIT_MARKET", quantity="0.01", stop_price="70000")
        assert bodies[-1]["triggerDirection"] == 1

        await c.place_order(
            "BTCUSDT", "BUY", "LIMIT", quantity="0.01", price=Decimal("64000"), time_in_force="GTX",
            reduce_only=True, client_order_id="cid-l", newOrderRespType="RESULT",
        )  # fmt: skip
        lim = bodies[-1]
        assert lim["orderType"] == "Limit" and lim["price"] == "64000" and lim["timeInForce"] == "PostOnly"
        assert lim["reduceOnly"] is True and "newOrderRespType" not in lim

        n = len(bodies)
        with pytest.raises(BybitAPIError):
            await c.place_order("BTCUSDT", "BUY", "MARKET", quantity="1", selfTradePreventionMode="X")
        with pytest.raises(UnsupportedFeatureError):
            await c.place_order("BTCUSDT", "BUY", "TRAILING_STOP_MARKET", quantity="1")
        with pytest.raises(BybitAPIError):
            await c.place_order("BTCUSDT", "BUY", "STOP_MARKET", quantity="1")  # no stop price
        assert len(bodies) == n  # nothing was sent

        with pytest.raises(ExchangeAPIError) as exc:
            await c.cancel_order("BTCUSDT", client_order_id="gone")
        assert exc.value.code == ERR_CANCEL_REJECTED
        with pytest.raises(ValueError):
            await c.cancel_order("BTCUSDT")

        assert (await c.set_leverage("BTCUSDT", 3)) == {"symbol": "BTCUSDT", "leverage": 3}  # no-change tolerated

        order = await c.get_order("BTCUSDT", client_order_id="cid-2")
        assert order["status"] == "FILLED" and order["executedQty"] == "0.01" and order["side"] == "BUY"
        assert order["type"] == "MARKET" and order["clientOrderId"] == "cid-2"
        with pytest.raises(ExchangeAPIError) as exc:
            await c.get_order("BTCUSDT", client_order_id="missing")
        assert exc.value.code == ERR_ORDER_NOT_FOUND


async def test_bybit_margin_mode_no_change_and_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v5/account/set-margin-mode":
            return httpx.Response(200, json={"retCode": 110026, "retMsg": "not modified", "result": {}})
        return httpx.Response(403, text="forbidden")

    async with bybit(handler) as c:
        assert (await c.set_margin_type("BTCUSDT", "ISOLATED"))["code"] == ERR_NO_CHANGE
        with pytest.raises(BybitAPIError) as exc:
            await c.get_open_orders()
    assert exc.value.status_code == 403


async def test_bybit_streams_not_supported_and_supervisor_skips(monkeypatch) -> None:
    from app.workers import streams

    async def noop(_):
        return None

    adapter = bybit(lambda r: ok({}))
    with pytest.raises(UnsupportedFeatureError):
        adapter.user_data_stream(noop)
    with pytest.raises(UnsupportedFeatureError):
        BybitLinearAdapter.market_stream(["btcusdt@markPrice@1s"], noop)

    monkeypatch.setattr(streams, "client_for_account", lambda account: adapter)
    sup = streams.StreamSupervisor()
    fake_account = SimpleNamespace(id=uuid.uuid4(), workspace_id=uuid.uuid4())
    assert sup._start_user_stream(fake_account) is None


def test_binance_streams_from_adapter() -> None:
    from app.services.binance_ws import MarketStream, UserDataStream

    async def noop(_):
        return None

    client = BinanceFuturesClient("k", "s", testnet=True)
    assert isinstance(client.user_data_stream(noop, name="u"), UserDataStream)
    stream = BinanceFuturesClient.market_stream(["btcusdt@markPrice@1s"], noop, testnet=True, name="m")
    assert isinstance(stream, MarketStream) and stream.name == "m"


def test_registry_rejects_adapter_without_id() -> None:
    class Nameless(BybitLinearAdapter):
        exchange_id = ""

    with pytest.raises(ValueError):
        registry.register_exchange(Nameless)
