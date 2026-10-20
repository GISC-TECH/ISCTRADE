import asyncio
import json
from decimal import Decimal as D

import httpx
import pytest
from sqlalchemy import select
from websockets.asyncio.server import serve

from app.models import ExecutionMode, Order, OrderSide, OrderStatus, OrderType, Position, Trade
from app.services.binance_client import BinanceAPIError, BinanceFuturesClient
from app.services.binance_ws import MarketStream, UserDataStream
from app.services.dashboard import build_dashboard
from app.services.execution import PaperExecutor
from app.services.reconciler import Reconciler
from tests.factories import make_account, make_user_workspace


def mock_client(handler) -> BinanceFuturesClient:
    return BinanceFuturesClient("k", "s", testnet=True, transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- REST client
async def test_client_endpoints_and_errors() -> None:
    seen: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append((request.method, request.url.path, params))
        path = request.url.path
        if path == "/fapi/v1/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "ETHUSDT",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "10000"},
                                {"filterType": "MIN_NOTIONAL", "notional": "20"},
                            ],
                        }
                    ]
                },
            )
        if path == "/fapi/v1/marginType":
            return httpx.Response(400, json={"code": -4046, "msg": "No need to change margin type."})
        if path == "/fapi/v1/leverage":
            return httpx.Response(200, json={"leverage": int(params["leverage"]), "symbol": params["symbol"]})
        if path == "/fapi/v1/income":
            return httpx.Response(
                200,
                json=[
                    {"incomeType": "REALIZED_PNL", "income": "12.5"},
                    {"incomeType": "COMMISSION", "income": "-0.5"},
                    {"incomeType": "TRANSFER", "income": "1000"},
                ],
            )
        if path == "/fapi/v1/listenKey":
            assert "signature" not in params
            return httpx.Response(200, json={"listenKey": "LK1"} if request.method == "POST" else {})
        if path == "/fapi/v1/order" and request.method == "DELETE":
            return httpx.Response(400, json={"code": -2011, "msg": "Unknown order sent."})
        return httpx.Response(404, json={"code": -1, "msg": "not mocked"})

    async with mock_client(handler) as c:
        f = await c.get_symbol_filters("ETHUSDT")
        assert f.min_notional == D(20) and f.tick_size == D("0.01")
        assert (await c.set_margin_type("ETHUSDT", "isolated"))["code"] == -4046
        assert (await c.set_leverage("ETHUSDT", 3))["leverage"] == 3
        assert await c.get_realized_pnl(0) == D("12.0")
        assert await c.create_listen_key() == "LK1"
        await c.keepalive_listen_key()
        with pytest.raises(BinanceAPIError) as exc:
            await c.cancel_order("ETHUSDT", order_id=1)
        assert exc.value.code == -2011
        with pytest.raises(ValueError):
            await c.cancel_order("ETHUSDT")
    signed = [p for m, path, p in seen if path in ("/fapi/v1/leverage", "/fapi/v1/income")]
    assert all("signature" in p and "timestamp" in p for p in signed)


async def test_public_client_refuses_signed_calls() -> None:
    async with BinanceFuturesClient(
        "", "", testnet=True, transport=httpx.MockTransport(lambda r: httpx.Response(200))
    ) as c:
        with pytest.raises(BinanceAPIError):
            await c.get_account()


async def test_klines_range_paginates() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["startTime"])
        calls.append(start)
        n = 1500 if len(calls) == 1 else 10
        return httpx.Response(
            200, json=[[start + i * 60000, "1", "1", "1", "1", "1", start + i * 60000 + 59999] for i in range(n)]
        )

    async with mock_client(handler) as c:
        rows = await c.get_klines_range("BTCUSDT", "1m", 0, 10**12)
    assert len(rows) == 1510 and len(calls) == 2


# --------------------------------------------------------------------------- WebSockets
async def test_market_stream_reconnects() -> None:
    connections = 0

    async def server_handler(ws):
        nonlocal connections
        connections += 1
        assert "streams=btcusdt@markprice@1s" in ws.request.path
        await ws.send(
            json.dumps({"stream": "x", "data": {"e": "markPriceUpdate", "s": "BTCUSDT", "p": str(connections)}})
        )
        if connections == 1:
            await ws.close()  # force a reconnect
        else:
            await ws.wait_closed()

    received: list[dict] = []
    async with serve(server_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]

        async def handler(msg):
            received.append(msg)
            if len(received) == 2:
                stream.stop()

        stream = MarketStream(
            ["BTCUSDT@markPrice@1s"], handler, base_url=f"ws://127.0.0.1:{port}", initial_backoff=0.05
        )
        await asyncio.wait_for(stream.run(), timeout=10)
    assert [m["p"] for m in received] == ["1", "2"]
    assert stream.connections == 2


async def test_user_data_stream_renews_listen_key_on_expiry() -> None:
    keys = iter(["KEY1", "KEY2", "KEY3"])
    created: list[str] = []

    def rest(request: httpx.Request) -> httpx.Response:
        key = next(keys)
        created.append(key)
        return httpx.Response(200, json={"listenKey": key})

    paths: list[str] = []

    async def server_handler(ws):
        paths.append(ws.request.path)
        if len(paths) == 1:
            await ws.send(json.dumps({"e": "listenKeyExpired"}))
            await ws.wait_closed()
        else:
            await ws.send(json.dumps({"e": "ORDER_TRADE_UPDATE", "o": {}}))
            await ws.wait_closed()

    events: list[dict] = []
    async with serve(server_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = mock_client(rest)

        async def handler(evt):
            events.append(evt)
            stream.stop()

        stream = UserDataStream(client, handler, base_url=f"ws://127.0.0.1:{port}", initial_backoff=0.05)
        await asyncio.wait_for(stream.run(), timeout=10)
        await client.close()
    assert paths == ["/ws/KEY1", "/ws/KEY2"]
    assert events[0]["e"] == "ORDER_TRADE_UPDATE"


# --------------------------------------------------------------------------- Reconciler
async def test_reconciler_syncs_positions_orders_and_trades(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws, execution_mode=ExecutionMode.LIVE)
    order = Order(
        workspace_id=ws.id, account_id=acc.id, symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.MARKET,
        status=OrderStatus.NEW, quantity=D("0.01"), client_order_id="btp-abc", exchange_order_id="77", is_paper=False,
    )  # fmt: skip
    db.add(order)
    await db.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/fapi/v2/positionRisk":
            return httpx.Response(
                200,
                json=[
                    {"symbol": "BTCUSDT", "positionAmt": "0.010", "entryPrice": "60000", "markPrice": "60500",
                     "unRealizedProfit": "5", "leverage": "3"},
                    {"symbol": "ETHUSDT", "positionAmt": "0", "entryPrice": "0", "markPrice": "3000",
                     "unRealizedProfit": "0", "leverage": "5"},
                ],
            )  # fmt: skip
        if path == "/fapi/v1/order":
            assert request.url.params["origClientOrderId"] == "btp-abc"
            return httpx.Response(
                200, json={"orderId": 77, "status": "FILLED", "executedQty": "0.010", "avgPrice": "60000"}
            )
        if path == "/fapi/v1/userTrades":
            return httpx.Response(
                200,
                json=[{"id": 9, "orderId": 77, "side": "BUY", "price": "60000", "qty": "0.010", "commission": "0.24",
                       "commissionAsset": "USDT", "realizedPnl": "0", "time": 1700000000000}],
            )  # fmt: skip
        return httpx.Response(404, json={"code": -1, "msg": path})

    async with mock_client(handler) as client:
        summary = await Reconciler(db, acc, client).run()
        again = await Reconciler(db, acc, client).run()
    await db.commit()
    assert summary == {"positions_changed": 1, "orders_updated": 1, "trades_inserted": 1}
    assert again["trades_inserted"] == 0  # idempotent
    pos = await db.scalar(select(Position).where(Position.symbol == "BTCUSDT"))
    assert pos.quantity == D("0.01") and pos.leverage == 3 and not pos.is_paper
    trade = await db.scalar(select(Trade))
    assert trade.order_id == order.id and trade.fee == D("0.24")
    assert order.status == OrderStatus.FILLED


async def test_reconciler_handles_stream_events(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws, execution_mode=ExecutionMode.LIVE)
    db.add(
        Order(
            workspace_id=ws.id, account_id=acc.id, symbol="BTCUSDT", side=OrderSide.SELL, type=OrderType.MARKET,
            status=OrderStatus.NEW, quantity=D("1"), client_order_id="btp-x", is_paper=False,
        )
    )  # fmt: skip
    await db.commit()
    rec = Reconciler(db, acc, client=None)  # type: ignore[arg-type]
    await rec.handle_event(
        {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1,
            "o": {"s": "BTCUSDT", "c": "btp-x", "S": "SELL", "x": "TRADE", "X": "FILLED", "i": 5, "l": "1",
                  "z": "1", "L": "100", "ap": "100", "n": "0.04", "N": "USDT", "T": 1700000000000, "t": 42,
                  "rp": "3.5"},
        }
    )  # fmt: skip
    await rec.handle_event(
        {"e": "ACCOUNT_UPDATE", "a": {"P": [{"s": "BTCUSDT", "pa": "-1", "ep": "100", "up": "0", "ps": "BOTH"}]}}
    )
    await db.commit()
    order = await db.scalar(select(Order))
    assert order.status == OrderStatus.FILLED and order.exchange_order_id == "5"
    trade = await db.scalar(select(Trade))
    assert trade.realized_pnl == D("3.5") and trade.exchange_trade_id == "42"
    assert (await db.scalar(select(Position))).quantity == D(-1)


# --------------------------------------------------------------------------- Dashboard
async def test_dashboard_paper(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    from app.services.execution import OrderService
    from app.services.risk_manager import AccountRiskState
    from app.strategy.base import OrderIntent

    rs = AccountRiskState(D(10000), D(10000), D(10000))
    svc = OrderService(PaperExecutor(fee_rate=D(0), slippage_bps=D(0)))
    await svc.submit(
        db,
        acc,
        OrderIntent(OrderSide.BUY, D(1), stop_loss=D(95)),
        symbol="BTCUSDT",
        ref_price=D(100),
        leverage=1,
        risk_state=rs,
    )
    await svc.submit(
        db,
        acc,
        OrderIntent(OrderSide.SELL, D("0.5"), reduce_only=True),
        symbol="BTCUSDT",
        ref_price=D(110),
        leverage=1,
        risk_state=rs,
    )
    await db.commit()
    data = await build_dashboard(db, acc)
    assert data["source"] == "paper"
    assert data["realized_pnl_today"] == D(5)
    assert data["open_positions"] == 1 and data["positions"][0]["quantity"] == D("0.5")
    assert data["wallet_balance"] == D(10005)


async def test_dashboard_live_from_exchange(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws, execution_mode=ExecutionMode.LIVE)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v2/account":
            return httpx.Response(
                200,
                json={"totalWalletBalance": "1000", "totalUnrealizedProfit": "-12.5", "availableBalance": "800",
                      "positions": [{"symbol": "ETHUSDT", "positionAmt": "0.5", "entryPrice": "3000",
                                     "unrealizedProfit": "-12.5", "leverage": "2"}]},
            )  # fmt: skip
        if request.url.path == "/fapi/v1/income":
            return httpx.Response(200, json=[{"incomeType": "REALIZED_PNL", "income": "7"}])
        return httpx.Response(404, json={})

    async with mock_client(handler) as client:
        data = await build_dashboard(db, acc, client)
    assert data["source"] == "exchange" and data["equity"] == D("987.5")
    assert data["realized_pnl_today"] == D(7) and data["positions"][0]["symbol"] == "ETHUSDT"

    async with mock_client(lambda r: httpx.Response(500, json={"code": -1000, "msg": "boom"})) as client:
        data = await build_dashboard(db, acc, client)
    assert data["error"] and data["source"] == "database"
