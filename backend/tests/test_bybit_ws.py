"""Bybit V5 WebSocket streams (experimental): translation to canonical events, auth, subscriptions, ping.

No network: ``websockets`` connect is replaced by an in-memory fake socket. No orders are ever sent.
"""

import asyncio
import hashlib
import hmac
import json

import pytest

from app.core.config import settings
from app.exchanges import bybit_ws
from app.exchanges.base import UnsupportedFeatureError
from app.exchanges.bybit import BybitLinearAdapter
from app.services import binance_ws
from app.services.reconciler import needs_protection_check
from app.workers.streams import user_event_summary


class FakeSocket:
    def __init__(self, owner, messages):
        self.owner, self.messages, self.sent = owner, list(messages), []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        await asyncio.sleep(0)
        if not self.messages:
            self.owner["stream"].stop()
            raise OSError("closed by test")
        return json.dumps(self.messages.pop(0))


@pytest.fixture
def fake_ws(monkeypatch):
    state: dict = {"sockets": [], "urls": []}

    def connect(url, **kw):
        state["urls"].append(url)
        sock = FakeSocket(state, state["script"])

        class Ctx:
            async def __aenter__(self):
                state["sockets"].append(sock)
                return sock

            async def __aexit__(self, *exc):
                return False

        return Ctx()

    monkeypatch.setattr(binance_ws, "ws_connect", connect)
    return state


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "BYBIT_WS_ENABLED", True)


def test_disabled_by_default() -> None:
    assert settings.BYBIT_WS_ENABLED is False
    with pytest.raises(UnsupportedFeatureError):
        BybitLinearAdapter.market_stream(["btcusdt@markPrice@1s"], None)


def test_topic_mapping() -> None:
    assert bybit_ws.topic_for("btcusdt@markPrice@1s") == ("tickers.BTCUSDT", "markPrice")
    assert bybit_ws.topic_for("ETHUSDT@miniTicker") == ("tickers.ETHUSDT", "miniTicker")
    assert bybit_ws.topic_for("btcusdt@kline_1h") == ("kline.60.BTCUSDT", "kline")
    assert bybit_ws.topic_for("btcusdt@kline_1d") == ("kline.D.BTCUSDT", "kline")
    for bad in ("!miniTicker@arr", "btcusdt@depth5", "btcusdt@kline_7m"):
        with pytest.raises(UnsupportedFeatureError):
            bybit_ws.topic_for(bad)


async def test_market_stream_snapshot_delta_and_klines(fake_ws, enabled) -> None:
    got: list[dict] = []

    async def handler(e):
        got.append(e)

    stream = BybitLinearAdapter.market_stream(
        ["btcusdt@markPrice@1s", "btcusdt@miniTicker", *[f"s{i}usdt@kline_1m" for i in range(10)]], handler
    )
    fake_ws["stream"] = stream
    fake_ws["script"] = [
        {"success": True, "op": "subscribe", "conn_id": "x"},
        {"topic": "tickers.BTCUSDT", "type": "snapshot", "ts": 1000, "data": {
            "symbol": "BTCUSDT", "markPrice": "60000.5", "indexPrice": "60001", "fundingRate": "0.0001",
            "nextFundingTime": "1700000000000", "lastPrice": "60000", "prevPrice24h": "59000", "highPrice24h": "61000",
            "lowPrice24h": "58000", "volume24h": "123", "turnover24h": "7380000"}},
        {"topic": "tickers.BTCUSDT", "type": "delta", "ts": 2000, "data": {"symbol": "BTCUSDT", "markPrice": "60010"}},
        {"topic": "kline.1.S0USDT", "type": "snapshot", "ts": 3000, "data": [{
            "start": 1672324800000, "end": 1672324859999, "interval": "1", "open": "1", "close": "2", "high": "3",
            "low": "0.5", "volume": "10", "turnover": "15", "confirm": True, "timestamp": 1672324860000}]},
        {"op": "pong", "ret_msg": "pong"},
    ]  # fmt: skip
    await asyncio.wait_for(stream.run(), 5)
    assert fake_ws["urls"] == [settings.BYBIT_TESTNET_WS_PUBLIC_URL]
    subs = [m for m in fake_ws["sockets"][0].sent if m["op"] == "subscribe"]
    assert [len(m["args"]) for m in subs] == [10, 1]  # 11 topics (tickers shared) in chunks of 10
    assert "tickers.BTCUSDT" in subs[0]["args"] and "kline.1.S9USDT" in subs[1]["args"]
    marks = [e for e in got if e["e"] == "markPriceUpdate"]
    assert marks[0] == {"e": "markPriceUpdate", "E": 1000, "s": "BTCUSDT", "p": "60000.5", "i": "60001",
                        "r": "0.0001", "T": 1700000000000}  # fmt: skip
    assert marks[1]["p"] == "60010" and marks[1]["r"] == "0.0001"  # delta merged over the snapshot
    minis = [e for e in got if e["e"] == "24hrMiniTicker"]
    assert len(minis) == 2 and minis[0]["c"] == "60000" and minis[0]["q"] == "7380000"
    [k] = [e for e in got if e["e"] == "kline"]
    assert k["s"] == "S0USDT" and k["k"]["i"] == "1m" and k["k"]["x"] is True and k["k"]["T"] == 1672324859999


async def test_private_stream_auth_subscribe_and_translation(fake_ws, enabled) -> None:
    got: list[dict] = []

    async def handler(e):
        got.append(e)

    adapter = BybitLinearAdapter("key-123", "secret-xyz", testnet=True)
    stream = adapter.user_data_stream(handler, name="user-1")
    assert stream.client is adapter
    fake_ws["stream"] = stream
    fake_ws["script"] = [
        {"success": True, "ret_msg": "", "op": "auth", "conn_id": "c"},
        {"success": True, "op": "subscribe"},
        {"topic": "order", "creationTime": 5, "data": [{
            "category": "linear", "symbol": "BTCUSDT", "orderId": "o1", "orderLinkId": "cid-1", "side": "Sell",
            "orderType": "Market", "orderStatus": "Cancelled", "qty": "0.01", "price": "0", "triggerPrice": "58000",
            "cumExecQty": "0", "avgPrice": "", "reduceOnly": True, "updatedTime": "1700000000001"}]},
        {"topic": "execution", "creationTime": 6, "data": [
            {"category": "linear", "symbol": "BTCUSDT", "orderId": "o2", "orderLinkId": "cid-2", "side": "Buy",
             "orderType": "Market", "execId": "e1", "execPrice": "60000", "execQty": "0.004", "orderQty": "0.01",
             "leavesQty": "0.006", "execFee": "0.13", "execPnl": "0", "execType": "Trade", "execTime": "1700000000002",
             "feeCurrency": "USDT"},
            {"category": "linear", "symbol": "BTCUSDT", "execType": "Funding", "orderId": "", "execId": "f1"},
            {"category": "spot", "symbol": "BTCUSDT", "execType": "Trade", "orderId": "s1", "execId": "s1"}]},
        {"topic": "position", "creationTime": 7, "data": [
            {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "size": "0.01", "positionIdx": 0,
             "entryPrice": "60000", "unrealisedPnl": "-1.5", "tradeMode": 0},
            {"category": "linear", "symbol": "ETHUSDT", "side": "Buy", "size": "1", "positionIdx": 1,
             "entryPrice": "3000", "unrealisedPnl": "2"}]},
        {"topic": "wallet", "creationTime": 8, "data": [{"accountType": "UNIFIED", "coin": [
            {"coin": "USDT", "walletBalance": "1000.5", "availableToWithdraw": "900"}, {"coin": "BTC"}]}]},
    ]  # fmt: skip
    await asyncio.wait_for(stream.run(), 5)
    await adapter.close()
    sock = fake_ws["sockets"][0]
    assert fake_ws["urls"] == [settings.BYBIT_TESTNET_WS_PRIVATE_URL]
    auth, sub = sock.sent[0], sock.sent[1]
    key, expires, sig = auth["args"]
    assert auth["op"] == "auth" and key == "key-123"
    assert sig == hmac.new(b"secret-xyz", f"GET/realtime{expires}".encode(), hashlib.sha256).hexdigest()
    assert sub == {"op": "subscribe", "args": list(bybit_ws.PRIVATE_TOPICS)} and stream.authenticated
    assert all(m["op"] in ("auth", "subscribe", "ping") for m in sock.sent)  # never an order over the socket

    order, trade, positions, wallet = got
    o = order["o"]
    assert order["e"] == "ORDER_TRADE_UPDATE" and o["X"] == "CANCELED" and o["o"] == "STOP_MARKET"
    assert o["c"] == "cid-1" and o["S"] == "SELL" and o["R"] is True
    assert needs_protection_check(order)  # a canceled stop triggers the stop guarantee, as on Binance
    t = trade["o"]
    assert t["x"] == "TRADE" and t["X"] == "PARTIALLY_FILLED" and t["t"] == "e1" and t["z"] == "0.004"
    assert (t["l"], t["L"], t["n"], t["rp"], t["T"]) == ("0.004", "60000", "0.13", "0", 1700000000002)
    p = positions["a"]["P"]
    assert p[0] == {"s": "BTCUSDT", "pa": "-0.01", "ep": "60000", "up": "-1.5", "mt": "cross", "ps": "BOTH"}
    assert p[1]["ps"] == "LONG"  # hedge-mode rows are skipped by the reconciler
    assert wallet["a"]["B"] == [{"a": "USDT", "wb": "1000.5", "cw": "900"}]
    summary = user_event_summary(positions)
    assert summary[0] == "account_update" and summary[1]["positions"][0]["amount"] == "-0.01"


async def test_private_auth_failure_reconnects(fake_ws, enabled) -> None:
    stream = bybit_ws.BybitUserDataStream("k", "s", lambda e: None, initial_backoff=0.01)
    fake_ws["stream"] = stream
    fake_ws["script"] = [{"success": False, "ret_msg": "error sign!", "op": "auth"}]

    calls = {"n": 0}
    orig = stream._dispatch

    async def dispatch(msg):
        calls["n"] += 1
        res = await orig(msg)
        if calls["n"] == 1:
            assert res is False and not stream.authenticated
            fake_ws["script"] = []  # second connection: socket closes, test stops the stream
        return res

    stream._dispatch = dispatch
    await asyncio.wait_for(stream.run(), 5)
    assert len(fake_ws["urls"]) == 2  # reconnected after the auth failure
    assert not any(m["op"] == "subscribe" for s in fake_ws["sockets"] for m in s.sent)


async def test_pinger_sends_app_level_ping() -> None:
    stream = bybit_ws.BybitMarketStream(["btcusdt@markPrice"], lambda e: None)
    sock = FakeSocket({}, [])
    stream.ping_seconds = 0.01
    await stream.on_open(sock)
    await asyncio.sleep(0.05)
    await stream.on_close()
    assert {"op": "ping"} in sock.sent and {"op": "subscribe", "args": ["tickers.BTCUSDT"]} in sock.sent
