"""Real-time WebSocket: auth via cookie, Origin check, workspace isolation, throttling, heartbeat, events.

Redis is unreachable in tests, so the in-process MemoryBroker carries the pub/sub messages.
The WebSocket is driven through a fake connection object on the test's event loop.
"""

import asyncio
import json
import time
import uuid

import pytest
from fastapi import WebSocketDisconnect

from app.api.v1.endpoints import realtime_ws
from app.core.config import settings
from app.core.security import create_access_token
from app.services import realtime
from app.services.audit import log_event
from app.workers.market_feed import MarketFeed, kline_row, mark_rows, ticker_rows
from tests.factories import make_user_workspace


class FakeWS:
    def __init__(self, token: str | None = None, origin: str | None = None) -> None:
        self.cookies = {"access_token": token} if token else {}
        self.headers = {"origin": origin} if origin else {}
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.accepted = False
        self.close_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, text: str) -> None:
        if self.close_code is not None:
            raise RuntimeError("closed")
        self.sent.append(json.loads(text))

    async def receive_text(self) -> str:
        item = await self.inbox.get()
        if item is None:
            raise WebSocketDisconnect(1000)
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.close_code is None:
            self.close_code = code
        self.inbox.put_nowait(None)

    def client_send(self, msg: dict) -> None:
        self.inbox.put_nowait(json.dumps(msg))

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.sent if m["type"] == kind]


async def wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(settings, "REALTIME_PRICE_THROTTLE_MS", 100)
    monkeypatch.setattr(settings, "REALTIME_HEARTBEAT_SECONDS", 0.3)
    monkeypatch.setattr(settings, "CORS_ORIGINS", ["https://app.example.com"])
    realtime_ws._connections.clear()


def _token(user) -> str:
    return create_access_token(str(user.id), {"ver": user.token_version})


async def _open(ws: FakeWS, workspace_id) -> asyncio.Task:
    task = asyncio.create_task(realtime_ws.realtime_ws(ws, workspace_id))
    await wait_for(lambda: ws.close_code is not None or ws.of_type("hello"))
    return task


async def _shutdown(ws: FakeWS, task: asyncio.Task) -> None:
    ws.inbox.put_nowait(None)
    await asyncio.wait_for(task, 3)


async def test_auth_origin_and_membership(db) -> None:
    user, ws_row = await make_user_workspace(db)
    other, other_ws = await make_user_workspace(db)
    await db.commit()

    bad_origin = FakeWS(_token(user), origin="https://evil.example.net")
    await realtime_ws.realtime_ws(bad_origin, ws_row.id)
    assert bad_origin.close_code == 4403 and not bad_origin.accepted  # rejected before the handshake

    anon = FakeWS(None, origin="https://app.example.com")
    await realtime_ws.realtime_ws(anon, ws_row.id)
    assert anon.close_code == 4401

    forged = FakeWS("not-a-jwt", origin="https://app.example.com")
    await realtime_ws.realtime_ws(forged, ws_row.id)
    assert forged.close_code == 4401

    stranger = FakeWS(_token(user), origin="https://app.example.com")
    await realtime_ws.realtime_ws(stranger, other_ws.id)  # not a member of that workspace
    assert stranger.close_code == 4404

    user.token_version += 1  # password change / logout everywhere revokes the old token
    stale_token = create_access_token(str(user.id), {"ver": user.token_version - 1})
    await db.commit()
    stale = FakeWS(stale_token)
    await realtime_ws.realtime_ws(stale, ws_row.id)
    assert stale.close_code == 4401

    ok = FakeWS(_token(user), origin="https://app.example.com/")
    task = await _open(ok, ws_row.id)
    hello = ok.of_type("hello")[0]["data"]
    assert hello["workspace_id"] == str(ws_row.id) and hello["broker"] == "memory"
    await _shutdown(ok, task)


async def test_workspace_isolation_and_global_events(db) -> None:
    user, ws_row = await make_user_workspace(db)
    _, other_ws = await make_user_workspace(db)
    await db.commit()
    conn = FakeWS(_token(user))
    task = await _open(conn, ws_row.id)

    await realtime.publish(realtime.workspace_channel(other_ws.id), realtime.message("order", {"secret": 1}))
    await realtime.publish(realtime.workspace_channel(ws_row.id), realtime.message("order", {"mine": 1}))
    await realtime.publish(realtime.GLOBAL, realtime.message("scanner", {"n": 3}))
    await wait_for(lambda: conn.of_type("order") and conn.of_type("scanner"))
    await asyncio.sleep(0.1)
    assert [m["data"] for m in conn.of_type("order")] == [{"mine": 1}]

    # audit events are published after commit (thread -> MemoryBroker) to the workspace channel only
    await log_event(db, "manual.close", workspace_id=ws_row.id, account_id=None, symbol="BTCUSDT")
    await log_event(db, "manual.close", workspace_id=other_ws.id, account_id=None, symbol="ETHUSDT")
    await db.commit()
    await wait_for(lambda: conn.of_type("manual"))
    await asyncio.sleep(0.2)
    assert [m["data"]["symbol"] for m in conn.of_type("manual")] == ["BTCUSDT"]

    # rolled back events are never published
    await log_event(db, "manual.leverage", workspace_id=ws_row.id, account_id=None, symbol="XRPUSDT")
    await db.rollback()
    await asyncio.sleep(0.2)
    assert all(m["data"]["symbol"] != "XRPUSDT" for m in conn.of_type("manual"))
    await _shutdown(conn, task)


async def test_market_throttle_subscription_heartbeat_and_ping(db, monkeypatch) -> None:
    wanted: list[tuple[bool, list[str]]] = []

    async def fake_want(testnet, streams):
        wanted.append((testnet, streams))

    monkeypatch.setattr(realtime, "want_klines", fake_want)
    user, ws_row = await make_user_workspace(db)
    await db.commit()
    conn = FakeWS(_token(user))
    task = await _open(conn, ws_row.id)

    conn.client_send({"op": "subscribe", "net": "main", "symbols": ["btcusdt"],
                      "klines": [{"symbol": "BTCUSDT", "interval": "1m"}]})  # fmt: skip
    await wait_for(lambda: conn.of_type("subscribed"))
    assert wanted == [(False, ["btcusdt@kline_1m"])]

    main = realtime.market_channel(False)
    for i in range(20):  # burst: 20 ticker batches within one throttle window
        rows = [{"s": "BTCUSDT", "c": 50000 + i}, {"s": "ETHUSDT", "c": 3000 + i}]
        await realtime.publish(main, realtime.message("tickers", rows))
    await realtime.publish(realtime.market_channel(True), realtime.message("tickers", [{"s": "BTCUSDT", "c": 1}]))
    await wait_for(lambda: conn.of_type("tickers"))
    await asyncio.sleep(0.25)
    tickers = conn.of_type("tickers")
    assert len(tickers) <= 2  # coalesced (<= one message per flush window)
    rows = [r for m in tickers for r in m["data"]]
    assert {r["s"] for r in rows} == {"BTCUSDT"}  # only subscribed symbols, only the subscribed network
    assert rows[-1]["c"] == 50019  # latest value wins

    k = {"s": "BTCUSDT", "i": "1m", "t": 1, "c": 1.0, "x": False}
    await realtime.publish(main, realtime.message("kline", {**k, "c": 1.5}))
    await realtime.publish(main, realtime.message("kline", {**k, "c": 2.0, "x": True}))
    await realtime.publish(main, realtime.message("kline", {**k, "t": 2, "c": 3.0}))
    await realtime.publish(main, realtime.message("kline", {**k, "i": "5m"}))  # not subscribed
    await wait_for(lambda: any(m["data"]["t"] == 2 for m in conn.of_type("kline")))
    klines = [m["data"] for m in conn.of_type("kline")]
    assert {"t": 1, "c": 2.0, "x": True} == {key: klines[0][key] for key in ("t", "c", "x")}  # closed candle kept
    assert all(m["i"] == "1m" for m in klines)

    conn.client_send({"op": "ping"})
    conn.client_send({"op": "subscribe", "net": "nope"})
    conn.inbox.put_nowait("{bad json")
    await wait_for(lambda: conn.of_type("pong") and len(conn.of_type("error")) == 2)
    await wait_for(lambda: conn.of_type("heartbeat"))
    await _shutdown(conn, task)


async def test_connection_limit_and_token_expiry(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "REALTIME_MAX_CONNECTIONS_PER_USER", 1)
    user, ws_row = await make_user_workspace(db)
    await db.commit()
    first = FakeWS(_token(user))
    task = await _open(first, ws_row.id)
    second = FakeWS(_token(user))
    await realtime_ws.realtime_ws(second, ws_row.id)
    assert second.close_code == 4429
    await _shutdown(first, task)
    assert realtime_ws._connections.get(user.id) is None

    real_verify = realtime_ws.verify_token
    monkeypatch.setattr(realtime_ws, "verify_token", lambda t: {**real_verify(t), "exp": time.time() + 0.3})
    expiring = FakeWS(_token(user))
    task = await _open(expiring, ws_row.id)
    await asyncio.wait_for(task, 3)
    assert expiring.close_code == 4401


async def test_recheck_closes_when_membership_revoked(db, monkeypatch) -> None:
    user, ws_row = await make_user_workspace(db)
    await db.commit()
    monkeypatch.setattr(realtime_ws, "RECHECK_SECONDS", 0.2)
    conn = FakeWS(_token(user))
    task = await _open(conn, ws_row.id)
    user.is_active = False
    await db.commit()
    await asyncio.wait_for(task, 3)
    assert conn.close_code == 4401


async def test_market_feed_handle_and_kline_throttle(monkeypatch) -> None:
    monkeypatch.setattr(settings, "REALTIME_PRICE_THROTTLE_MS", 10_000)
    out: list[tuple[str, dict]] = []

    async def publisher(channel, msg):
        out.append((channel, msg))
        return True

    feed = MarketFeed(testnet=True, publisher=publisher, cache_marks=False)
    await feed.handle(
        [{"e": "24hrMiniTicker", "s": "BTCUSDT", "c": "110", "o": "100", "h": "111", "l": "99", "q": "5"}]
    )
    await feed.handle([{"e": "markPriceUpdate", "s": "BTCUSDT", "p": "110.5", "r": "0.0001", "T": 123}])
    kl = {"e": "kline", "s": "BTCUSDT", "k": {"s": "BTCUSDT", "i": "1m", "t": 1, "T": 2, "o": "1", "h": "2",
                                               "l": "0.5", "c": "1.5", "v": "10", "x": False}}  # fmt: skip
    await feed.handle(kl)
    await feed.handle(kl)  # throttled (open candle)
    await feed.handle({**kl, "k": {**kl["k"], "x": True}})  # closed candle always published
    kinds = [m["type"] for _, m in out]
    assert kinds == ["tickers", "mark", "kline", "kline"]
    assert all(ch == "rt:market:test" for ch, _ in out)
    assert out[0][1]["data"][0]["P"] == 10.0
    assert mark_rows([{"s": "X", "p": "1", "r": "2", "T": 3}]) == [{"s": "X", "p": 1.0, "r": 2.0, "T": 3}]
    assert ticker_rows([{"s": "X", "c": "1", "o": "0"}])[0]["P"] is None
    assert kline_row(kl)["x"] is False


async def test_market_feed_sync_restarts_on_new_klines(monkeypatch) -> None:
    started: list[list[str]] = []

    class FakeStream:
        def __init__(self, streams, handler, testnet, name):
            started.append(streams)

        async def run(self):
            await asyncio.sleep(3600)

        def stop(self):
            return None

    wanted: set[str] = set()

    async def fake_wanted(testnet):
        return set(wanted)

    monkeypatch.setattr(realtime, "wanted_klines", fake_wanted)
    feed = MarketFeed(testnet=False, publisher=None, stream_factory=FakeStream)
    assert await feed.sync() is True
    assert await feed.sync() is False
    wanted.add("btcusdt@kline_1m")
    assert await feed.sync() is True
    assert started[-1][-1] == "btcusdt@kline_1m"
    feed.stop()


def test_audit_mapping() -> None:
    assert realtime.AUDIT_TYPES["manual.order"] == "manual"
    msg = realtime.message("tickers", [{"s": "X"}], net="main")
    assert msg["type"] == "tickers" and msg["net"] == "main" and "ts" in msg
    assert realtime.workspace_channel(uuid.UUID(int=1)).startswith("rt:ws:")


def test_user_event_summary_has_no_secrets() -> None:
    from app.workers.streams import user_event_summary

    order = {"e": "ORDER_TRADE_UPDATE", "o": {"s": "BTCUSDT", "S": "BUY", "o": "LIMIT", "X": "FILLED", "x": "TRADE",
                                              "l": "0.01", "L": "50000", "ap": "50000", "rp": "0", "R": False,
                                              "c": "btp-1"}}  # fmt: skip
    kind, data = user_event_summary(order)
    assert kind == "order_update" and data["status"] == "FILLED" and data["last_price"] == "50000"
    acc = {"e": "ACCOUNT_UPDATE", "a": {"m": "ORDER", "B": [{"a": "USDT", "wb": "10"}, {"a": "BNB", "wb": "1"}],
                                        "P": [{"s": "BTCUSDT", "pa": "0.01", "ep": "50000", "up": "1"}]}}  # fmt: skip
    kind, data = user_event_summary(acc)
    assert kind == "account_update" and data["balances"] == [{"asset": "USDT", "wallet": "10"}]
    assert data["positions"][0]["amount"] == "0.01"
    assert user_event_summary({"e": "listenKeyExpired"}) is None


async def test_klines_endpoint_uses_public_client(client, monkeypatch) -> None:
    from app.api.v1.endpoints import market

    calls: list[tuple] = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get_klines(self, symbol, interval, limit):
            calls.append((symbol, interval, limit))
            now = int(time.time() * 1000)
            return [[1, "1", "2", "0.5", "1.5", "10", 2], [now, "1.5", "1.6", "1.4", "1.55", "3", now + 60_000]]

    monkeypatch.setattr(market, "market_client", lambda testnet: FakeClient())
    pw = "s3nha-forte-123"
    await client.post("/api/v1/auth/register", json={"email": "k@example.com", "password": pw, "full_name": "K"})
    await client.post("/api/v1/auth/login", json={"email": "k@example.com", "password": pw})
    r = await client.get("/api/v1/market/klines", params={"symbol": "btcusdt", "interval": "1m", "limit": 50})
    assert r.status_code == 200, r.text
    assert calls == [("BTCUSDT", "1m", 50)]
    assert r.json()[0] == {"t": 1, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 10.0, "x": True}
    assert r.json()[1]["x"] is False
    assert (await client.get("/api/v1/market/klines", params={"symbol": "BTC", "interval": "7m"})).status_code == 422


async def test_market_hub_decodes_once_for_all_sessions(db, monkeypatch) -> None:
    monkeypatch.setattr(realtime, "want_klines", lambda *a, **k: asyncio.sleep(0))
    user, ws_row = await make_user_workspace(db)
    await db.commit()
    decoded: list[str] = []
    real_decode = realtime_ws.decode_market
    monkeypatch.setattr(realtime_ws, "decode_market", lambda data: decoded.append(data) or real_decode(data))

    conns = [FakeWS(_token(user)) for _ in range(3)]
    tasks = [await _open(c, ws_row.id) for c in conns]
    for c, net in zip(conns, ("main", "main", "test"), strict=True):
        c.client_send({"op": "subscribe", "net": net, "symbols": ["BTCUSDT"]})
        await wait_for(lambda c=c: c.of_type("subscribed"))
    market_subs = [s for s in realtime.memory_broker._subs if realtime.market_channel(False) in s[2]]
    assert len(market_subs) == 1  # one shared market subscription for the whole process

    rows = [{"s": f"S{i}USDT", "c": i} for i in range(300)] + [{"s": "BTCUSDT", "c": 42}]
    await realtime.publish(realtime.market_channel(False), realtime.message("tickers", rows))
    await wait_for(lambda: conns[0].of_type("tickers") and conns[1].of_type("tickers"))
    await asyncio.sleep(0.15)
    assert len(decoded) == 1  # decoded once, not once per session
    assert [r["s"] for m in conns[0].of_type("tickers") for r in m["data"]] == ["BTCUSDT"]
    assert not conns[2].of_type("tickers")  # other network

    for c, t in zip(conns, tasks, strict=True):
        await _shutdown(c, t)
    hub = realtime_ws.market_hub()
    assert hub.task is None and not hub.listeners  # stopped with the last session
    assert not [s for s in realtime.memory_broker._subs if realtime.market_channel(False) in s[2]]
