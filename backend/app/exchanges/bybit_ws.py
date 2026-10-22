"""Bybit V5 WebSocket streams (linear USDT perpetuals) — EXPERIMENTAL, off by default (``BYBIT_WS_ENABLED``).

Both streams reuse the reconnecting loop of :class:`app.services.binance_ws.BinanceStream` (exponential backoff,
max connection age, idle timeout, disconnect alerts) and translate Bybit's native messages into the **canonical
Binance-shaped events** that the streams worker, the reconciler and the real-time panel already understand:

Public (``wss://stream.bybit.com/v5/public/linear``), canonical stream names in, canonical events out:

* ``<sym>@markPrice`` / ``<sym>@markPrice@1s`` -> topic ``tickers.<SYM>`` -> ``markPriceUpdate``
  (``s``, ``p`` mark, ``i`` index, ``r`` funding rate, ``T`` next funding time);
* ``<sym>@miniTicker`` -> ``tickers.<SYM>`` -> ``24hrMiniTicker`` (``c``, ``o``, ``h``, ``l``, ``v``, ``q``);
* ``<sym>@kline_<interval>`` -> ``kline.<bybit interval>.<SYM>`` -> ``kline`` (``k.t/T/o/h/l/c/v/q/x/i``).

Ticker topics send a snapshot and then *deltas* with only the changed fields, so the last full state per symbol
is merged before translating. All-market streams (``!miniTicker@arr``, ``!markPrice@arr``) have no Bybit
equivalent and are refused.

Private (``wss://stream.bybit.com/v5/private``): ``auth`` (HMAC-SHA256 of ``"GET/realtime" + expires``), then
``order.linear``, ``execution.linear``, ``position.linear`` and ``wallet``:

* order -> ``ORDER_TRADE_UPDATE`` with ``x`` = the canonical status (no fill fields);
* execution (``execType=Trade``) -> ``ORDER_TRADE_UPDATE`` with ``x=TRADE`` (``t`` exec id, ``l``/``L``, ``n``
  fee, ``rp`` = ``execPnl``, ``z`` = ``orderQty - leavesQty``);
* position -> ``ACCOUNT_UPDATE`` ``a.P`` (signed ``pa``; ``ps=BOTH`` only for one-way ``positionIdx=0``);
* wallet -> ``ACCOUNT_UPDATE`` ``a.B`` (USDT wallet balance).

Bybit requires an application-level ``{"op": "ping"}`` every 20 s. No order is ever sent over these sockets.
Docs: https://bybit-exchange.github.io/docs/v5/ws/connect
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal
from typing import Any

from app.core.config import settings
from app.exchanges.base import StreamHandler, UnsupportedFeatureError
from app.services.binance_ws import BinanceStream

logger = logging.getLogger(__name__)

PING_SECONDS = 20.0
SUBSCRIBE_CHUNK = 10  # args per subscribe request
PRIVATE_TOPICS = ("order.linear", "execution.linear", "position.linear", "wallet")
# Binance interval -> Bybit interval (same table as the REST adapter)
INTERVALS = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "2h": "120", "4h": "240",
    "6h": "360", "12h": "720", "1d": "D", "1w": "W", "1M": "M",
}  # fmt: skip
INTERVALS_BACK = {v: k for k, v in INTERVALS.items()}
ORDER_STATUS = {
    "Created": "NEW", "New": "NEW", "Untriggered": "NEW", "Triggered": "NEW", "Active": "NEW",
    "PartiallyFilled": "PARTIALLY_FILLED", "Filled": "FILLED", "Cancelled": "CANCELED",
    "PartiallyFilledCanceled": "CANCELED", "Deactivated": "CANCELED", "Rejected": "REJECTED",
}  # fmt: skip


def public_url(testnet: bool) -> str:
    return settings.BYBIT_TESTNET_WS_PUBLIC_URL if testnet else settings.BYBIT_WS_PUBLIC_URL


def private_url(testnet: bool) -> str:
    return settings.BYBIT_TESTNET_WS_PRIVATE_URL if testnet else settings.BYBIT_WS_PRIVATE_URL


def require_enabled(feature: str) -> None:
    if not settings.BYBIT_WS_ENABLED:
        raise UnsupportedFeatureError(f"{feature} (set BYBIT_WS_ENABLED=true, experimental)", "bybit_linear")


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal(0)
    except ArithmeticError:
        return Decimal(0)


def _fmt(d: Decimal) -> str:
    return format(d.normalize(), "f") if d else "0"


# ---------------------------------------------------------------------------- public: names and translation
def topic_for(stream: str) -> tuple[str, str]:
    """Canonical Binance stream name -> (Bybit topic, kind). Raises UnsupportedFeatureError for unknown kinds."""
    if stream.startswith("!"):
        raise UnsupportedFeatureError(f"all-market stream {stream}", "bybit_linear")
    symbol, _, rest = stream.partition("@")
    sym = symbol.upper()
    if rest in ("markPrice", "markPrice@1s"):
        return f"tickers.{sym}", "markPrice"
    if rest == "miniTicker":
        return f"tickers.{sym}", "miniTicker"
    if rest.startswith("kline_") and rest[6:] in INTERVALS:
        return f"kline.{INTERVALS[rest[6:]]}.{sym}", "kline"
    raise UnsupportedFeatureError(f"stream {stream}", "bybit_linear")


def ticker_events(t: dict[str, Any], kinds: set[str], ts: int) -> list[dict[str, Any]]:
    sym = t.get("symbol")
    out: list[dict[str, Any]] = []
    if "markPrice" in kinds and t.get("markPrice"):
        out.append({
            "e": "markPriceUpdate", "E": ts, "s": sym, "p": t.get("markPrice"), "i": t.get("indexPrice") or "0",
            "r": t.get("fundingRate") or "0", "T": int(t.get("nextFundingTime") or 0),
        })  # fmt: skip
    if "miniTicker" in kinds and t.get("lastPrice"):
        out.append({
            "e": "24hrMiniTicker", "E": ts, "s": sym, "c": t.get("lastPrice"), "o": t.get("prevPrice24h") or "0",
            "h": t.get("highPrice24h") or "0", "l": t.get("lowPrice24h") or "0", "v": t.get("volume24h") or "0",
            "q": t.get("turnover24h") or "0",
        })  # fmt: skip
    return out


def kline_event(symbol: str, k: dict[str, Any], ts: int) -> dict[str, Any]:
    return {
        "e": "kline", "E": ts, "s": symbol,
        "k": {
            "t": int(k["start"]), "T": int(k["end"]), "s": symbol, "i": INTERVALS_BACK.get(str(k.get("interval")), ""),
            "o": k.get("open"), "h": k.get("high"), "l": k.get("low"), "c": k.get("close"), "v": k.get("volume"),
            "q": k.get("turnover"), "x": bool(k.get("confirm")),
        },
    }  # fmt: skip


# ---------------------------------------------------------------------------- private translation
def order_event(o: dict[str, Any], ts: int) -> dict[str, Any]:
    status = ORDER_STATUS.get(o.get("orderStatus", ""), "NEW")
    otype = str(o.get("orderType", "")).upper()
    if o.get("triggerPrice") not in (None, "", "0"):
        otype = "STOP_MARKET" if otype == "MARKET" else "STOP"
    return {
        "e": "ORDER_TRADE_UPDATE", "E": ts, "T": int(o.get("updatedTime") or ts),
        "o": {
            "s": o.get("symbol"), "c": o.get("orderLinkId") or "", "i": o.get("orderId"),
            "S": str(o.get("side", "")).upper(), "o": otype, "ot": otype, "X": status, "x": status,
            "q": o.get("qty", "0"), "p": o.get("price", "0"), "sp": o.get("triggerPrice", "0"),
            "z": o.get("cumExecQty", "0"), "ap": o.get("avgPrice") or "0", "R": bool(o.get("reduceOnly")),
            "T": int(o.get("updatedTime") or ts),
        },
    }  # fmt: skip


def execution_event(x: dict[str, Any], ts: int) -> dict[str, Any] | None:
    if x.get("execType") != "Trade":
        return None  # funding / ADL / liquidation bookkeeping rows are reconciled over REST
    order_qty, leaves = _dec(x.get("orderQty")), _dec(x.get("leavesQty"))
    filled = order_qty - leaves
    return {
        "e": "ORDER_TRADE_UPDATE", "E": ts, "T": int(x.get("execTime") or ts),
        "o": {
            "s": x.get("symbol"), "c": x.get("orderLinkId") or "", "i": x.get("orderId"),
            "S": str(x.get("side", "")).upper(), "o": str(x.get("orderType", "")).upper(),
            "X": "FILLED" if leaves == 0 else "PARTIALLY_FILLED", "x": "TRADE", "t": x.get("execId"),
            "l": x.get("execQty", "0"), "L": x.get("execPrice", "0"), "z": _fmt(filled),
            "n": x.get("execFee", "0"), "N": x.get("feeCurrency") or "USDT", "rp": x.get("execPnl") or "0",
            "m": bool(x.get("isMaker")), "T": int(x.get("execTime") or ts),
        },
    }  # fmt: skip


def position_entry(p: dict[str, Any]) -> dict[str, Any]:
    size = _dec(p.get("size"))
    signed = -size if p.get("side") == "Sell" else size
    idx = int(p.get("positionIdx") or 0)
    return {
        "s": p.get("symbol"), "pa": _fmt(signed), "ep": p.get("entryPrice") or p.get("avgPrice") or "0",
        "up": p.get("unrealisedPnl") or "0", "mt": "isolated" if str(p.get("tradeMode")) == "1" else "cross",
        "ps": {0: "BOTH", 1: "LONG", 2: "SHORT"}.get(idx, "BOTH"),
    }  # fmt: skip


def wallet_entries(w: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"a": c.get("coin"), "wb": c.get("walletBalance") or "0", "cw": c.get("availableToWithdraw") or "0"}
        for c in w.get("coin") or []
        if c.get("coin") == "USDT"
    ]


# ---------------------------------------------------------------------------- streams
class _BybitStream(BinanceStream):
    """Bybit sockets need an app-level ping every 20 s; subscriptions are sent after each (re)connect."""

    ping_seconds = PING_SECONDS

    def __init__(self, handler: StreamHandler, **kw: Any) -> None:
        super().__init__(handler, **kw)
        self._ws = None
        self._ping_task: asyncio.Task | None = None

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(payload))

    async def _pinger(self) -> None:
        while True:
            await asyncio.sleep(self.ping_seconds)
            try:
                await self._send({"op": "ping"})
            except Exception as exc:  # the read loop notices the broken socket and reconnects
                logger.debug("bybit ping failed: %s", exc)
                return

    async def on_open(self, ws) -> None:
        self._ws = ws
        self._ping_task = asyncio.create_task(self._pinger())

    async def on_close(self) -> None:
        if self._ping_task:
            self._ping_task.cancel()
            self._ping_task = None
        self._ws = None

    async def _subscribe(self, topics: list[str]) -> None:
        for i in range(0, len(topics), SUBSCRIBE_CHUNK):
            await self._send({"op": "subscribe", "args": topics[i : i + SUBSCRIBE_CHUNK]})

    @staticmethod
    def _control(msg: dict[str, Any]) -> bool:
        """Responses to ping/subscribe/auth carry ``op`` and no ``topic``."""
        return "topic" not in msg and ("op" in msg or "success" in msg)


class BybitMarketStream(_BybitStream):
    idle_timeout: float | None = 60.0

    def __init__(
        self, streams: list[str], handler: StreamHandler, *, testnet: bool = True, base_url: str | None = None, **kw
    ) -> None:
        super().__init__(handler, name=kw.pop("name", "bybit-market"), **kw)
        self.streams = list(streams)
        self.base_url = base_url or public_url(testnet)
        self.kinds: dict[str, set[str]] = {}
        for s in self.streams:
            topic, kind = topic_for(s)
            self.kinds.setdefault(topic, set()).add(kind)
        self._tickers: dict[str, dict[str, Any]] = {}

    async def url(self) -> str:
        return self.base_url

    async def on_open(self, ws) -> None:
        await super().on_open(ws)
        self._tickers.clear()  # a new snapshot follows every subscription
        await self._subscribe(list(self.kinds))

    async def _dispatch(self, msg: dict[str, Any]) -> bool | None:
        if self._control(msg):
            if msg.get("op") == "subscribe" and msg.get("success") is False:
                logger.warning("bybit subscribe failed: %s", msg.get("ret_msg"))
            return None
        topic = str(msg.get("topic", ""))
        ts = int(msg.get("ts") or time.time() * 1000)
        data = msg.get("data")
        if topic.startswith("tickers.") and isinstance(data, dict):
            sym = topic.split(".", 1)[1]
            state = {} if msg.get("type") == "snapshot" else self._tickers.get(sym, {})
            state = {**state, **{k: v for k, v in data.items() if v not in (None, "")}}
            self._tickers[sym] = state
            for event in ticker_events(state, self.kinds.get(topic, set()), ts):
                await self.handler(event)
        elif topic.startswith("kline.") and isinstance(data, list):
            sym = topic.rsplit(".", 1)[1]
            for k in data:
                await self.handler(kline_event(sym, k, ts))
        return None


class BybitUserDataStream(_BybitStream):
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        handler: StreamHandler,
        *,
        testnet: bool = True,
        base_url: str | None = None,
        client: Any = None,
        **kw: Any,
    ) -> None:
        super().__init__(handler, name=kw.pop("name", "bybit-user"), **kw)
        self.client = client  # owning adapter (the streams worker closes it on shutdown)
        self._key, self._secret = api_key, api_secret
        self.base_url = base_url or private_url(testnet)
        self.authenticated = False

    def auth_payload(self, expires_ms: int | None = None) -> dict[str, Any]:
        expires = expires_ms or int((time.time() + 10) * 1000)
        sig = hmac.new(self._secret.encode(), f"GET/realtime{expires}".encode(), hashlib.sha256).hexdigest()
        return {"op": "auth", "args": [self._key, expires, sig]}

    async def url(self) -> str:
        return self.base_url

    async def on_open(self, ws) -> None:
        await super().on_open(ws)
        self.authenticated = False
        await self._send(self.auth_payload())

    async def _dispatch(self, msg: dict[str, Any]) -> bool | None:
        if self._control(msg):
            if msg.get("op") == "auth":
                if not msg.get("success"):
                    logger.warning("bybit private auth failed: %s", msg.get("ret_msg"))
                    return False  # reconnect with backoff (new signature)
                self.authenticated = True
                await self._subscribe(list(PRIVATE_TOPICS))
            elif msg.get("op") == "subscribe" and msg.get("success") is False:
                logger.warning("bybit private subscribe failed: %s", msg.get("ret_msg"))
            return None
        topic = str(msg.get("topic", ""))
        ts = int(msg.get("creationTime") or msg.get("ts") or time.time() * 1000)
        rows = [r for r in msg.get("data") or [] if r.get("category") in (None, "", "linear")]
        if topic.startswith("order"):
            for o in rows:
                await self.handler(order_event(o, ts))
        elif topic.startswith("execution"):
            for x in rows:
                event = execution_event(x, ts)
                if event is not None:
                    await self.handler(event)
        elif topic.startswith("position") and rows:
            await self.handler(
                {"e": "ACCOUNT_UPDATE", "E": ts, "a": {"m": "ORDER", "B": [], "P": [position_entry(p) for p in rows]}}
            )
        elif topic == "wallet":
            balances = [b for w in msg.get("data") or [] for b in wallet_entries(w)]
            if balances:
                await self.handler({"e": "ACCOUNT_UPDATE", "E": ts, "a": {"m": "WALLET", "B": balances, "P": []}})
        return None


__all__ = ["BybitMarketStream", "BybitUserDataStream", "topic_for"]
