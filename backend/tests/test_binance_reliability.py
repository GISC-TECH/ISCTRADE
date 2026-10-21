"""Binance client reliability: Algo Order API routing, unknown-status recovery, clock resync, GET retries."""

import httpx
import pytest

from app.services import binance_client as bc
from app.services.binance_client import BinanceAPIError, BinanceFuturesClient
from app.services.binance_http import rate_limits


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    rate_limits.reset()
    monkeypatch.setattr(bc, "RECOVERY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(bc, "GET_RETRY_BACKOFF", 0)


def client(handler) -> BinanceFuturesClient:
    return BinanceFuturesClient("k", "s", testnet=True, transport=httpx.MockTransport(handler))


async def test_conditional_orders_use_algo_api_and_are_normalized() -> None:
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, dict(req.url.params)))
        if req.url.path == "/fapi/v1/order":
            return httpx.Response(400, json={"code": -4120, "msg": "use algo"})
        return httpx.Response(
            200, json={"algoId": 77, "clientAlgoId": "btp-x", "algoStatus": "NEW", "orderType": "TAKE_PROFIT_MARKET"}
        )

    async with client(handler) as c:
        res = await c.place_order("BTCUSDT", "SELL", "TAKE_PROFIT_MARKET", stop_price="70000", close_position=True,
                                  client_order_id="btp-x", workingType="MARK_PRICE")  # fmt: skip
    method, path, params = seen[0]
    assert (method, path) == ("POST", "/fapi/v1/algoOrder")
    assert (
        params["triggerPrice"] == "70000" and params["clientAlgoId"] == "btp-x" and params["algoType"] == "CONDITIONAL"
    )
    assert res["orderId"] == 77 and res["status"] == "NEW" and res["isAlgo"]


async def test_get_and_cancel_fall_back_to_algo_orders() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/fapi/v1/order":
            return httpx.Response(400, json={"code": -2013 if req.method == "GET" else -2011, "msg": "Unknown order"})
        if req.url.path == "/fapi/v1/algoOrder":
            status = "TRIGGERED" if req.method == "GET" else "CANCELED"
            return httpx.Response(200, json={"algoId": 5, "clientAlgoId": "btp-s", "algoStatus": status})
        raise AssertionError(req.url.path)

    async with client(handler) as c:
        assert (await c.get_order("BTCUSDT", client_order_id="btp-s"))["status"] == "FILLED"
        assert (await c.cancel_order("BTCUSDT", client_order_id="btp-s"))["status"] == "CANCELED"


async def test_unknown_status_is_resolved_by_client_order_id_instead_of_duplicating() -> None:
    posts = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal posts
        if req.method == "POST":
            posts += 1
            raise httpx.ReadTimeout("timeout", request=req)  # sent, but no answer
        return httpx.Response(
            200, json={"orderId": 9, "clientOrderId": "btp-1", "status": "FILLED", "executedQty": "1"}
        )

    async with client(handler) as c:
        res = await c.place_order("BTCUSDT", "BUY", "MARKET", quantity="1", client_order_id="btp-1")
    assert res["orderId"] == 9 and posts == 1


async def test_unknown_status_and_not_found_reraises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(503, json={"code": -1007, "msg": "Send status unknown"})
        return httpx.Response(400, json={"code": -2013, "msg": "Order does not exist."})

    async with client(handler) as c:
        with pytest.raises(BinanceAPIError) as err:
            await c.place_order("BTCUSDT", "BUY", "MARKET", quantity="1", client_order_id="btp-2")
    assert err.value.code == -1007


async def test_clock_skew_triggers_time_resync_and_one_retry() -> None:
    attempts = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 10**13})
        attempts.append(int(req.url.params["timestamp"]))
        if len(attempts) == 1:
            return httpx.Response(400, json={"code": -1021, "msg": "Timestamp outside recvWindow"})
        return httpx.Response(200, json=[])

    async with client(handler) as c:
        assert await c.get_open_orders("BTCUSDT") == []
        assert c.time_offset_ms > 0 and attempts[1] > attempts[0]


async def test_reads_retry_transient_errors_but_writes_do_not() -> None:
    gets = posts = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal gets, posts
        if req.method == "GET":
            gets += 1
            if gets < 3:
                raise httpx.ConnectError("reset", request=req)
            return httpx.Response(200, json={"serverTime": 1})
        posts += 1
        return httpx.Response(502, text="bad gateway")

    async with client(handler) as c:
        assert await c.server_time() == 1
        with pytest.raises(BinanceAPIError):
            await c.set_leverage("BTCUSDT", 2)
    assert gets == 3 and posts == 1
