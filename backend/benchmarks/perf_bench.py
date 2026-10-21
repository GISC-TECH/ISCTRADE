"""Micro-benchmarks for the trading hot path (docs/AUDIT.md, section "Desempenho").

Never touches a real exchange account: Binance is replaced by ``httpx.MockTransport`` with a simulated
round-trip latency, except the optional ``http`` scenario, which only calls the public ``/fapi/v1/ping``.

Usage (from ``backend/``)::

    BENCH_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/bench_db \
    BENCH_REDIS_URL=redis://localhost:6379/15 python -m benchmarks.perf_bench [scenario ...]

Scenarios: order_live, paper_tick, task_overhead, ws_fanout, publish, endpoints, http (network).
The database is dropped/recreated with ``Base.metadata.create_all``: use a dedicated database.
"""

from __future__ import annotations

import base64
import os
import secrets
import statistics
import sys
import time

os.environ.setdefault("MASTER_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))
os.environ["DATABASE_URL"] = os.environ.get("BENCH_DATABASE_URL", "sqlite+aiosqlite:////tmp/btp-bench.db")
os.environ["REDIS_URL"] = os.environ.get("BENCH_REDIS_URL", "redis://127.0.0.1:6379/15")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ["LIVE_TRADING_ENABLED"] = "false"  # flipped in-process for the mocked live scenario only
os.environ["CELERY_DISPATCH_ENABLED"] = "false"
os.environ["WHATSAPP_ENABLED"] = "false"
os.environ["LOG_LEVEL"] = "ERROR"

import asyncio  # noqa: E402
from decimal import Decimal as D  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import httpx  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import AsyncSessionLocal, engine  # noqa: E402
from app.models import Base, ExecutionMode, OrderSide  # noqa: E402

RTT = float(os.environ.get("BENCH_RTT_MS", "25")) / 1000


def report(name: str, samples: list[float], unit: str = "ms", extra: str = "") -> None:
    samples = sorted(samples)
    p95 = samples[max(0, int(len(samples) * 0.95) - 1)]
    print(f"{name:<44} n={len(samples):<4} median={statistics.median(samples):8.2f}{unit} p95={p95:8.2f}{unit} {extra}")


class QueryCounter:
    def __init__(self) -> None:
        self.n = 0
        event.listen(engine.sync_engine, "before_cursor_execute", self._inc)

    def _inc(self, *_a, **_k) -> None:
        self.n += 1


async def reset_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


def mock_exchange(rtt: float = RTT) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(rtt)
        q = dict(request.url.params)
        headers = {"X-MBX-USED-WEIGHT-1M": "10"}
        if request.url.path.endswith("/order"):
            if q.get("type") == "MARKET":
                body = {"orderId": 1, "status": "FILLED", "executedQty": q.get("quantity"), "avgPrice": "100.5"}
                return httpx.Response(200, json=body, headers=headers)
            return httpx.Response(200, json={"orderId": 2, "status": "NEW"}, headers=headers)
        return httpx.Response(200, json={}, headers=headers)

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------- scenarios
async def order_live(n: int = 30) -> None:
    """Signal -> RiskManager -> entry + SL + TP on the (mocked) exchange."""
    from app.services.binance_client import BinanceFuturesClient
    from app.services.execution import LiveExecutor, OrderService
    from app.services.risk_manager import AccountRiskState
    from app.strategy.base import OrderIntent
    from tests.factories import make_account, make_user_workspace

    await reset_db()
    settings.LIVE_TRADING_ENABLED = True
    counter = QueryCounter()
    try:
        async with AsyncSessionLocal() as db:
            _, ws = await make_user_workspace(db)
            acc = await make_account(db, ws, execution_mode=ExecutionMode.LIVE)
            await db.commit()
            client = BinanceFuturesClient("k", "s", testnet=True, transport=mock_exchange())
            svc = OrderService(LiveExecutor(client))
            e = D("10000")
            rs = AccountRiskState(equity=e, peak_equity=e, day_start_equity=e, max_leverage=5)
            samples, queries = [], []
            for _ in range(n):
                q0, t0 = counter.n, time.perf_counter()
                res = await svc.submit(
                    db, acc, OrderIntent(OrderSide.BUY, D("0.5"), stop_loss=D("95")), symbol="BTCUSDT",
                    ref_price=D("100"), leverage=2, risk_state=rs, take_profit=D("110"),
                )  # fmt: skip
                await db.commit()
                samples.append((time.perf_counter() - t0) * 1000)
                queries.append(counter.n - q0)
                assert res.ok, res.reason
            await client.close()
        report(
            f"order_live (entry+SL+TP, RTT {RTT * 1000:.0f}ms)", samples, extra=f"queries={statistics.median(queries)}"
        )
    finally:
        settings.LIVE_TRADING_ENABLED = False


async def paper_tick(n: int = 40) -> None:
    """Full paper bot tick (market data -> strategy -> risk -> fill) with a fill on every tick."""
    from app.services.bot_runner import BotRunner
    from app.services.execution import PaperExecutor
    from app.services.market_data import StaticMarketData
    from tests.factories import candles_from_closes, make_account, make_bot, make_user_workspace

    await reset_db()
    counter = QueryCounter()
    grid = {"lower_price": 90, "upper_price": 110, "levels": 5, "quantity_per_level": 0.5, "stop_loss_pct": 5}
    async with AsyncSessionLocal() as db:
        _, ws = await make_user_workspace(db)
        acc = await make_account(db, ws)
        bot = await make_bot(db, acc, "grid", grid)
        await db.commit()
        closes = [101.0]
        market = StaticMarketData({"BTCUSDT": candles_from_closes(closes)})
        from app.services.grid_bot import GridBot

        runner_cls = GridBot if bot.strategy_key == "grid" else BotRunner
        runner = runner_cls(db, bot, acc, market, executor=PaperExecutor(slippage_bps=D(0)))
        await runner.tick()
        await db.commit()
        samples, queries, fills = [], [], 0
        for i in range(n):
            closes.append(99.0 if i % 2 == 0 else 105.5)
            market.candles["BTCUSDT"] = candles_from_closes(closes)
            q0, t0 = counter.n, time.perf_counter()
            res = await runner.tick()
            await db.commit()
            samples.append((time.perf_counter() - t0) * 1000)
            queries.append(counter.n - q0)
            fills += res.filled
    report("paper_tick (1 fill/tick)", samples, extra=f"queries={statistics.median(queries)} fills={fills}")


def task_overhead(n: int = 100) -> None:
    """Per-task cost of the Celery async bridge: 1 SELECT + 1 Redis GET per task."""
    from sqlalchemy import text

    from app.core.redis import get_redis
    from app.tasks.bot_tasks import run_async

    async def job(sessions):
        async with sessions() as db:
            await db.execute(text("select 1"))
        try:
            await get_redis().get("bench:none")
        except Exception:
            pass

    run_async(job)  # warm-up
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        run_async(job)
        samples.append((time.perf_counter() - t0) * 1000)
    report("celery run_async (SELECT 1 + Redis GET)", samples)


def ws_fanout(n_sessions: int = 50, n_msgs: int = 50) -> None:
    """API WebSocket fan-out: one all-market tickers message (600 symbols) coalesced by N sessions."""
    from app.api.v1.endpoints import realtime_ws
    from app.services import realtime

    rows = [{"s": f"SYM{i}USDT", "c": 1.0 + i, "o": 1.0, "h": 2.0, "l": 0.5, "q": 1e6, "P": 0.1} for i in range(600)]
    payload = realtime.encode(realtime.message("tickers", rows))

    async def run() -> list[float]:
        sessions = []
        for _ in range(n_sessions):
            s = realtime_ws.RealtimeSession(
                SimpleNamespace(), SimpleNamespace(id="u"), SimpleNamespace(id="w"), 0, time.time() + 3600
            )
            s.symbols = {f"SYM{i}USDT" for i in range(0, 600, 30)}  # 20 watched symbols
            sessions.append(s)
        samples, hub_samples = [], []
        for _ in range(n_msgs):
            t0 = time.perf_counter()
            for s in sessions:
                s._coalesce(payload)  # per-session decode (pre-MarketHub behaviour)
            samples.append((time.perf_counter() - t0) * 1000)
            if hasattr(realtime_ws, "decode_market"):
                t0 = time.perf_counter()
                update = realtime_ws.decode_market(payload)  # MarketHub: decode once per process
                for s in sessions:
                    s.on_market(*update)
                hub_samples.append((time.perf_counter() - t0) * 1000)
        return samples, hub_samples

    samples, hub_samples = asyncio.run(run())
    report(f"ws_fanout per-session decode ({n_sessions} x 600)", samples, extra=f"msg={len(payload)}B")
    if hub_samples:
        report(f"ws_fanout MarketHub ({n_sessions} x 600)", hub_samples)


def publish(n: int = 200) -> None:
    """After-commit realtime publish (sync path used by every committed order/fill)."""
    from app.services import realtime

    items = [("rt:ws:bench-perf", realtime.message("order", {"event": "order.filled", "qty": D("1")}))]
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        realtime._publish_sync(items)
        samples.append((time.perf_counter() - t0) * 1000)
    report("realtime _publish_sync (1 event)", samples)


async def endpoints(n: int = 50) -> None:
    from httpx import ASGITransport, AsyncClient

    from app.core import rate_limiting
    from app.main import app
    from app.services.execution import OrderService, PaperExecutor
    from app.services.risk_manager import AccountRiskState
    from app.strategy.base import OrderIntent

    await reset_db()
    rate_limiting.reset_memory_limiter()
    counter = QueryCounter()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://bench") as c:
        pw = "s3nha-forte-123"
        await c.post("/api/v1/auth/register", json={"email": "bench@example.com", "password": pw, "full_name": "B"})
        login = (await c.post("/api/v1/auth/login", json={"email": "bench@example.com", "password": pw})).json()
        c.headers["Authorization"] = f"Bearer {login['access_token']}"
        async with AsyncSessionLocal() as db:
            from sqlalchemy import select

            from app.models import User
            from tests.factories import make_account

            user = await db.scalar(select(User))
            ws = SimpleNamespace(id=user.active_workspace_id)
            acc = await make_account(db, ws)
            svc = OrderService(PaperExecutor())
            e = D("100000")
            rs = AccountRiskState(equity=e, peak_equity=e, day_start_equity=e, max_leverage=5)
            for i in range(200):
                await svc.submit(
                    db, acc, OrderIntent(OrderSide.BUY if i % 2 == 0 else OrderSide.SELL, D("0.1"), stop_loss=None,
                                         reduce_only=i % 2 == 1), symbol=f"S{(i // 2) % 10}USDT", ref_price=D("100"),
                    leverage=1, risk_state=rs,
                ) if i % 2 == 1 else await svc.submit(
                    db, acc, OrderIntent(OrderSide.BUY, D("0.1"), stop_loss=D("95")), symbol=f"S{(i // 2) % 10}USDT",
                    ref_price=D("100"), leverage=1, risk_state=rs,
                )  # fmt: skip
            await db.commit()
            acc_id = acc.id
        for path in (
            f"/api/v1/accounts/{acc_id}/dashboard",
            "/api/v1/history/orders?limit=50",
            "/api/v1/history/trades?limit=50",
            "/api/v1/history/positions",
            "/api/v1/accounts/",
        ):
            samples, queries = [], []
            for _ in range(n):
                q0, t0 = counter.n, time.perf_counter()
                r = await c.get(path)
                samples.append((time.perf_counter() - t0) * 1000)
                queries.append(counter.n - q0)
                assert r.status_code == 200, (path, r.status_code, r.text[:200])
            report(f"GET {path.split('?')[0].replace(str(acc_id), '{id}')}", samples,
                   extra=f"queries={statistics.median(queries)}")  # fmt: skip


async def http(n: int = 15) -> None:
    """Public /fapi/v1/ping with a fresh client per call (the pattern used by every tick/task)."""
    from app.services.binance_client import BinanceAPIError, BinanceFuturesClient

    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        async with BinanceFuturesClient("", "", testnet=False) as c:
            try:
                await c.ping()
            except BinanceAPIError:
                pass  # e.g. 451 from restricted locations: the TLS/HTTP round trip is still measured
        samples.append((time.perf_counter() - t0) * 1000)
    report("binance client per call: GET /fapi/v1/ping", samples)


ASYNC = {"order_live": order_live, "paper_tick": paper_tick, "endpoints": endpoints, "http": http}
SYNC = {"task_overhead": task_overhead, "ws_fanout": ws_fanout, "publish": publish}


async def _run(fn) -> None:
    try:
        await fn()
    finally:
        await engine.dispose()  # pooled connections are bound to this event loop


def main(argv: list[str]) -> None:
    names = argv or ["order_live", "paper_tick", "endpoints", "task_overhead", "ws_fanout", "publish"]
    for name in names:
        if name in ASYNC:
            asyncio.run(_run(ASYNC[name]))
        else:
            SYNC[name]()


if __name__ == "__main__":
    main(sys.argv[1:])
