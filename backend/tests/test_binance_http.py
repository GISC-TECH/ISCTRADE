"""Pooled HTTP client and Binance request-weight guard (no network: MockTransport only)."""

import time

import httpx
import pytest

from app.core.config import settings
from app.services.binance_client import BinanceAPIError, BinanceFuturesClient
from app.services.binance_http import rate_limits, shared_client


def mock_client(handler) -> BinanceFuturesClient:
    return BinanceFuturesClient("k", "s", testnet=True, transport=httpx.MockTransport(handler))


async def test_clients_share_one_pooled_http_client_per_loop() -> None:
    a = BinanceFuturesClient("key-a", "s", testnet=True)
    b = BinanceFuturesClient("", "", testnet=True)
    assert a._client is b._client is shared_client(a.base_url)
    await a.close()  # closing a client instance never closes the shared pool
    assert not shared_client(a.base_url).is_closed
    other = BinanceFuturesClient("", "", testnet=False)
    assert other._client is not a._client


async def test_api_key_is_sent_per_request_not_per_pool() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-MBX-APIKEY"))
        return httpx.Response(200, json={})

    async with mock_client(handler) as c:
        await c.ping()
        await c.get_balance()
    assert seen == ["k", "k"]


async def test_used_weight_header_is_tracked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={}, headers={"X-MBX-USED-WEIGHT-1M": "321"})

    async with mock_client(handler) as c:
        await c.ping()
        assert rate_limits.used(c.base_url) == 321


@pytest.mark.parametrize("status", [429, 418])
async def test_ban_fails_fast_without_calling_binance(status) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(status, json={"code": -1003, "msg": "Too many requests"}, headers={"Retry-After": "30"})

    async with mock_client(handler) as c:
        with pytest.raises(BinanceAPIError) as first:
            await c.get_klines("BTCUSDT")
        assert first.value.status_code == status
        for call in (c.get_klines("BTCUSDT"), c.place_order("BTCUSDT", "BUY", "MARKET", quantity="1")):
            with pytest.raises(BinanceAPIError) as again:
                await call
            assert again.value.code == -1003 and "paused" in again.value.message
    assert calls == ["/fapi/v1/klines"]  # nothing else reached the exchange during the ban
    assert rate_limits.state(c.base_url).banned_until > time.time() + 25


async def test_soft_limit_fails_fast_for_market_data_but_never_blocks_orders(monkeypatch) -> None:
    monkeypatch.setattr(settings, "BINANCE_WEIGHT_MAX_WAIT_SECONDS", 0.0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"orderId": 1, "status": "NEW"}, headers={"X-MBX-USED-WEIGHT-1M": "2300"})

    async with mock_client(handler) as c:
        await c.ping()  # 2300 >= 90% of 2400
        with pytest.raises(BinanceAPIError) as exc:
            await c.get_klines("BTCUSDT")
        assert exc.value.status_code == 429 and "weight guard" in exc.value.message
        await c.place_order("BTCUSDT", "SELL", "MARKET", quantity="1", reduce_only=True)
        await c.cancel_all_orders("BTCUSDT")
    assert calls == ["/fapi/v1/ping", "/fapi/v1/order", "/fapi/v1/allOpenOrders", "/fapi/v1/algoOpenOrders"]


async def test_soft_limit_waits_for_next_window_when_short(monkeypatch) -> None:
    import app.services.binance_http as bh

    monkeypatch.setattr(settings, "BINANCE_WEIGHT_MAX_WAIT_SECONDS", 120.0)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(bh.asyncio, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[], headers={"X-MBX-USED-WEIGHT-1M": "2390"})

    async with mock_client(handler) as c:
        await c.ping()
        await c.get_klines("BTCUSDT")
    assert len(slept) == 1 and 0 < slept[0] <= 60.5


async def test_exchange_info_is_cached_for_pooled_clients(monkeypatch) -> None:
    import app.services.binance_client as bc

    calls = []

    async def fake_request(self, method, path, params=None, signed=False, api_key_only=False):
        calls.append(path)
        return {"symbols": [{"symbol": "BTCUSDT", "filters": []}]}

    monkeypatch.setattr(bc.BinanceFuturesClient, "_request", fake_request)
    monkeypatch.setattr(bc, "_exchange_info_raw", {})
    monkeypatch.setattr(bc, "_exchange_info_cache", {})
    a, b = BinanceFuturesClient("", "", testnet=True), BinanceFuturesClient("", "", testnet=True)
    assert (await a.get_exchange_info())["symbols"][0]["symbol"] == "BTCUSDT"
    await b.get_exchange_info()
    assert (await b.get_symbol_filters("BTCUSDT")).symbol == "BTCUSDT"
    assert calls == ["/fapi/v1/exchangeInfo"]  # one download shared by scanner and filters
