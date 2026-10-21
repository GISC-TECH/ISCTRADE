"""Real-time WebSocket: `/api/v1/ws?workspace_id=<uuid>`.

* Auth: the same httpOnly access-token cookie as the REST API (browsers send it on the upgrade request).
  The Origin header must be one of CORS_ORIGINS (protects against cross-site WebSocket hijacking).
* Isolation: only `rt:ws:<workspace_id>` of a workspace the user belongs to, plus public market data and
  non-sensitive global events. Membership and session revocation are re-checked periodically; the socket
  is closed with 4401 when the access token expires (the client refreshes and reconnects).
* Throttling: price/kline updates are coalesced per symbol and flushed every REALTIME_PRICE_THROTTLE_MS.

Client -> server: {"op": "subscribe", "net": "main"|"test", "symbols": [...], "klines": [{"symbol","interval"}]}
                  {"op": "ping"}
Server -> client: hello, heartbeat, status, tickers, mark, kline, order, position, bot, account, alert, ...
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.api.deps import ACCESS_COOKIE_NAME, resolve_workspace, workspace_role
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.redis import get_redis
from app.core.security import TokenError, verify_token
from app.models import User, Workspace
from app.services import realtime

logger = logging.getLogger(__name__)
router = APIRouter()

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")
INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"}
MAX_SYMBOLS = 60
MAX_KLINES = 4
RECHECK_SECONDS = 60
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_TOO_MANY = 4429

_connections: dict[uuid.UUID, int] = defaultdict(int)


def origin_allowed(origin: str | None) -> bool:
    if not origin:  # non-browser clients do not send Origin (and cannot be driven cross-site)
        return True
    allowed = {o.rstrip("/") for o in settings.CORS_ORIGINS}
    return origin.rstrip("/") in allowed


class Subscription:
    """Redis pub/sub with an in-process fallback (MemoryBroker) when Redis is unreachable."""

    def __init__(self, channels: set[str]) -> None:
        self.channels = channels
        self.pubsub = None
        self.queue: asyncio.Queue | None = None
        self.redis_ok = False

    async def open(self) -> None:
        try:
            pubsub = get_redis().pubsub()
            await asyncio.wait_for(pubsub.subscribe(*self.channels), timeout=1)
            self.pubsub, self.redis_ok = pubsub, True
        except Exception as exc:
            logger.debug("realtime: redis pub/sub unavailable (%s); using in-process broker", type(exc).__name__)
            self.queue = realtime.memory_broker.subscribe(self.channels)

    async def get(self, timeout: float) -> tuple[str, str] | None:
        if self.pubsub is not None:
            msg = await self.pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
            if msg and msg.get("type") == "message":
                return msg["channel"], msg["data"]
            return None
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)  # type: ignore[union-attr]
        except TimeoutError:
            return None

    async def close(self) -> None:
        if self.pubsub is not None:
            with contextlib.suppress(Exception):
                await self.pubsub.aclose()
        if self.queue is not None:
            realtime.memory_broker.unsubscribe(self.queue)


class RealtimeSession:
    def __init__(self, ws: WebSocket, user: User, workspace: Workspace, token_ver: int, token_exp: float) -> None:
        self.ws = ws
        self.user_id = user.id
        self.workspace_id = workspace.id
        self.token_ver = token_ver
        self.token_exp = token_exp
        self.net = "main"
        self.symbols: set[str] = set()
        self.klines: set[tuple[str, str]] = set()
        self.pending: dict[tuple[str, str], dict[str, Any]] = {}
        self.ws_channel = realtime.workspace_channel(workspace.id)
        self.sub = Subscription(
            {realtime.market_channel(False), realtime.market_channel(True), self.ws_channel, realtime.GLOBAL}
        )
        self.closed = False
        self.close_code = 1000

    async def send(self, msg: dict[str, Any]) -> None:
        await self.ws.send_text(realtime.encode(msg))

    async def run(self) -> None:
        await self.sub.open()
        await self.send(
            realtime.message("hello", {
                "workspace_id": self.workspace_id, "realtime": True,
                "broker": "redis" if self.sub.redis_ok else "memory",
                "heartbeat_seconds": settings.REALTIME_HEARTBEAT_SECONDS,
                "throttle_ms": settings.REALTIME_PRICE_THROTTLE_MS,
            })
        )  # fmt: skip
        tasks = [asyncio.create_task(t) for t in (self._reader(), self._pump(), self._ticker())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception() and not isinstance(t.exception(), WebSocketDisconnect):
                    logger.warning("realtime session error: %s", type(t.exception()).__name__)
        finally:
            for t in tasks:
                t.cancel()
            await self.sub.close()

    async def close(self, code: int, reason: str = "") -> None:
        if not self.closed:
            self.closed, self.close_code = True, code
            with contextlib.suppress(Exception):
                await self.ws.close(code=code, reason=reason)

    # ------------------------------------------------------------------ client -> server
    async def _reader(self) -> None:
        while not self.closed:
            raw = await self.ws.receive_text()
            if len(raw) > 4096:
                await self.send(realtime.message("error", {"detail": "message too large"}))
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await self.send(realtime.message("error", {"detail": "invalid JSON"}))
                continue
            op = msg.get("op") if isinstance(msg, dict) else None
            if op == "ping":
                await self.send(realtime.message("pong"))
            elif op == "subscribe":
                await self._subscribe(msg)
            else:
                await self.send(realtime.message("error", {"detail": f"unknown op {op!r}"}))

    async def _subscribe(self, msg: dict[str, Any]) -> None:
        net = msg.get("net", self.net)
        symbols = {str(s).upper() for s in (msg.get("symbols") or []) if isinstance(s, str)}
        symbols = {s for s in symbols if SYMBOL_RE.match(s)}
        klines = set()
        for k in msg.get("klines") or []:
            if isinstance(k, dict):
                sym, itv = str(k.get("symbol", "")).upper(), str(k.get("interval", ""))
                if SYMBOL_RE.match(sym) and itv in INTERVALS:
                    klines.add((sym, itv))
        if net not in ("main", "test") or len(symbols) > MAX_SYMBOLS or len(klines) > MAX_KLINES:
            await self.send(realtime.message("error", {"detail": "invalid subscription"}))
            return
        self.net, self.symbols, self.klines = net, symbols, klines
        self.pending.clear()
        await self._register_klines()
        await self.send(
            realtime.message("subscribed", {"net": net, "symbols": sorted(symbols),
                                            "klines": [{"symbol": s, "interval": i} for s, i in sorted(klines)]})
        )  # fmt: skip

    async def _register_klines(self) -> None:
        if self.klines:
            await realtime.want_klines(self.net == "test", [f"{s.lower()}@kline_{i}" for s, i in self.klines])

    # ------------------------------------------------------------------ server -> client
    async def _pump(self) -> None:
        market = realtime.market_channel(self.net == "test")
        while not self.closed:
            item = await self.sub.get(timeout=1.0)
            if item is None:
                continue
            channel, data = item
            if channel.startswith("rt:market:"):
                market = realtime.market_channel(self.net == "test")
                if channel == market:
                    self._coalesce(data)
            elif channel in (self.ws_channel, realtime.GLOBAL):
                await self.ws.send_text(data)

    def _coalesce(self, data: str) -> None:
        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            return
        kind = msg.get("type")
        if kind in ("tickers", "mark"):
            for row in msg.get("data") or []:
                if row.get("s") in self.symbols:
                    self.pending[(kind, row["s"])] = row
        elif kind == "kline":
            row = msg.get("data") or {}
            if (row.get("s"), row.get("i")) in self.klines:
                key = ("kline", f"{row['s']}:{row['i']}")
                prev = self.pending.get(key)
                if prev is not None and prev.get("x") and prev.get("t") != row.get("t"):
                    # never drop a closed candle: flush it on its own before the next one overwrites it
                    self.pending[("kline_closed", key[1] + f":{prev['t']}")] = prev
                self.pending[key] = row

    async def _flush(self) -> None:
        if not self.pending:
            return
        batch, self.pending = self.pending, {}
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for (kind, _), row in batch.items():
            grouped["kline" if kind == "kline_closed" else kind].append(row)
        for kind in ("tickers", "mark"):
            if grouped.get(kind):
                await self.send(realtime.message(kind, grouped[kind], net=self.net))
        for row in sorted(grouped.get("kline", []), key=lambda r: r.get("t") or 0):
            await self.send(realtime.message("kline", row, net=self.net))

    async def _ticker(self) -> None:
        interval = max(settings.REALTIME_PRICE_THROTTLE_MS, 50) / 1000
        last_beat = last_check = last_klines = time.monotonic()
        while not self.closed:
            await asyncio.sleep(interval)
            await self._flush()
            now = time.monotonic()
            if time.time() >= self.token_exp:
                await self.close(CLOSE_UNAUTHORIZED, "token expired")
                return
            if now - last_beat >= settings.REALTIME_HEARTBEAT_SECONDS:
                last_beat = now
                await self.send(realtime.message("heartbeat", {"broker": "redis" if self.sub.redis_ok else "memory"}))
            if now - last_klines >= settings.REALTIME_KLINE_TTL_SECONDS / 2:
                last_klines = now
                await self._register_klines()
            if now - last_check >= RECHECK_SECONDS:
                last_check = now
                code = await self._recheck()
                if code:
                    await self.close(code, "access revoked")
                    return

    async def _recheck(self) -> int | None:
        async with AsyncSessionLocal() as db:
            user = await db.get(User, self.user_id)
            if user is None or not user.is_active or user.token_version != self.token_ver:
                return CLOSE_UNAUTHORIZED
            workspace = await db.get(Workspace, self.workspace_id)
            if workspace is None or await workspace_role(db, workspace, user) is None:
                return CLOSE_FORBIDDEN
        return None


async def authenticate(ws: WebSocket, workspace_id: uuid.UUID | None) -> tuple[User, Workspace, dict] | int:
    token = ws.cookies.get(ACCESS_COOKIE_NAME)
    if not token:
        return CLOSE_UNAUTHORIZED
    try:
        payload = verify_token(token)
        user_id = uuid.UUID(payload["sub"])
    except (TokenError, KeyError, ValueError):
        return CLOSE_UNAUTHORIZED
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if user is None or not user.is_active or payload.get("ver", 0) != user.token_version:
            return CLOSE_UNAUTHORIZED
        try:
            ctx = await resolve_workspace(db, user, workspace_id)
        except HTTPException:
            return CLOSE_NOT_FOUND
        return user, ctx.workspace, payload


@router.websocket("/ws")
async def realtime_ws(websocket: WebSocket, workspace_id: uuid.UUID | None = None) -> None:
    if not settings.REALTIME_ENABLED:
        await websocket.close(code=1013)
        return
    if not origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=CLOSE_FORBIDDEN)  # before accept -> HTTP 403
        return
    auth = await authenticate(websocket, workspace_id)
    await websocket.accept()
    if isinstance(auth, int):
        await websocket.close(code=auth)
        return
    user, workspace, payload = auth
    if _connections[user.id] >= settings.REALTIME_MAX_CONNECTIONS_PER_USER:
        await websocket.close(code=CLOSE_TOO_MANY)
        return
    _connections[user.id] += 1
    session = RealtimeSession(websocket, user, workspace, int(payload.get("ver", 0)), float(payload.get("exp", 0)))
    try:
        await session.run()
    except WebSocketDisconnect:
        pass
    finally:
        _connections[user.id] -= 1
        if _connections[user.id] <= 0:
            _connections.pop(user.id, None)
        await session.close(session.close_code)
