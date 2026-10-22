"""Prometheus metrics.

Process-local counters/histograms are defined here. The API process exposes them on GET /metrics together
with gauges computed from the database at scrape time (DatabaseCollector). The Celery worker (multiprocess
mode via PROMETHEUS_MULTIPROC_DIR) and the streams worker expose their own metrics on METRICS_PORT.
"""

from __future__ import annotations

import logging
import os
import re

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily

logger = logging.getLogger(__name__)

HTTP_REQUESTS = Counter("btp_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "btp_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
ORDERS = Counter("btp_orders_total", "Orders processed by OrderService", ["mode", "result", "reason"])
BOT_TICKS = Counter("btp_bot_ticks_total", "Bot ticks by result", ["result"])
RECONCILER_CHANGES = Counter(
    "btp_reconciler_changes_total", "Local state corrected by the reconciler (drift vs exchange)", ["kind"]
)
WS_RECONNECTS = Counter("btp_ws_reconnects_total", "WebSocket (re)connections after the first", ["stream"])
WS_CONNECTED = Gauge("btp_ws_connected", "WebSocket connected (1) or not (0)", ["stream"], multiprocess_mode="max")
CELERY_TASKS = Counter("btp_celery_tasks_total", "Celery tasks by final state", ["task", "status"])
CELERY_TASK_DURATION = Histogram(
    "btp_celery_task_duration_seconds",
    "Celery task duration",
    ["task"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
BINANCE_USED_WEIGHT = Gauge(
    "btp_binance_used_weight_1m",
    "Binance IP request weight used in the current minute",
    ["host"],
    multiprocess_mode="max",
)
BINANCE_RATE_LIMITED = Counter(
    "btp_binance_rate_limited_total", "HTTP 429 (rate limit) / 418 (IP ban) answers from Binance", ["host", "status"]
)
WS_LAST_MESSAGE_AGE = Gauge(
    "btp_ws_last_message_age_seconds", "Seconds since the last message of a WebSocket (-1 = never)", ["stream", "kind"]
)
ALERTS = Counter("btp_alerts_total", "Alerts by event and delivery status", ["event", "status"])


def reason_label(reason: str | None) -> str:
    """Bounded-cardinality label from a rejection reason ('max_daily_loss: 3.1% >= 3%' -> 'max_daily_loss')."""
    if not reason:
        return "none"
    head = re.split(r"[:\s]", reason.strip(), maxsplit=1)[0].lower()
    return re.sub(r"[^a-z0-9_]", "_", head)[:40] or "other"


def safe_inc(counter: Counter, amount: float = 1, **labels: str) -> None:
    try:
        counter.labels(**labels).inc(amount)
    except Exception:  # metrics must never break business logic
        logger.debug("metric update failed", exc_info=True)


class DatabaseCollector:
    """Exposes the latest DB snapshot (set by the /metrics endpoint before rendering) as gauges."""

    def __init__(self) -> None:
        self.snapshot: dict = {}

    def collect(self):
        snap = self.snapshot
        g = GaugeMetricFamily("btp_bots", "Bots by state", labels=["state"])
        for state, n in snap.get("bots", {}).items():
            g.add_metric([state], n)
        yield g
        g = GaugeMetricFamily("btp_accounts", "Accounts by status and mode", labels=["status", "mode"])
        for (status, mode), n in snap.get("accounts", {}).items():
            g.add_metric([status, mode], n)
        yield g
        labels = ["account_id", "workspace_id", "mode"]
        eq = GaugeMetricFamily("btp_account_equity_usdt", "Last known account equity", labels=labels)
        pnl = GaugeMetricFamily("btp_account_daily_pnl_usdt", "Equity change since the daily baseline", labels=labels)
        dd = GaugeMetricFamily("btp_account_drawdown_pct", "Drawdown from peak equity", labels=labels)
        pos = GaugeMetricFamily("btp_account_open_positions", "Open positions", labels=labels)
        for acc in snap.get("account_rows", []):
            key = [acc["id"], acc["workspace_id"], acc["mode"]]
            if acc.get("equity") is not None:
                eq.add_metric(key, acc["equity"])
            if acc.get("daily_pnl") is not None:
                pnl.add_metric(key, acc["daily_pnl"])
            if acc.get("drawdown_pct") is not None:
                dd.add_metric(key, acc["drawdown_pct"])
            pos.add_metric(key, acc.get("open_positions", 0))
        yield from (eq, pnl, dd, pos)
        yield GaugeMetricFamily(
            "btp_global_kill_switch", "Global kill switch active", value=1 if snap.get("global_kill") else 0
        )
        age = snap.get("streams_heartbeat_age")
        yield GaugeMetricFamily(
            "btp_streams_heartbeat_age_seconds", "Seconds since the streams worker heartbeat (-1 = never)",
            value=-1 if age is None else age,
        )  # fmt: skip
        age = snap.get("beat_heartbeat_age")
        yield GaugeMetricFamily(
            "btp_beat_heartbeat_age_seconds", "Seconds since the Celery beat heartbeat (-1 = never)",
            value=-1 if age is None else age,
        )  # fmt: skip
        g = GaugeMetricFamily(
            "btp_worker_heartbeat_age_seconds", "Seconds since ops.heartbeat ran on the queue (-1 = never)",
            labels=["queue"],
        )  # fmt: skip
        for queue, age in snap.get("worker_heartbeat_age", {}).items():
            g.add_metric([queue], -1 if age is None else age)
        yield g
        age = snap.get("bot_tick_age")
        yield GaugeMetricFamily(
            "btp_bot_last_tick_age_seconds", "Seconds since the last completed bot tick (-1 = never)",
            value=-1 if age is None else age,
        )  # fmt: skip
        g = GaugeMetricFamily("btp_alerts_last_24h", "Alerts recorded in the last 24h", labels=["status"])
        for status, n in snap.get("alerts_24h", {}).items():
            g.add_metric([status], n)
        yield g


DB_COLLECTOR = DatabaseCollector()
_db_collector_registered = False


def register_db_collector() -> None:
    """Called by the API process only (workers do not expose DB gauges)."""
    global _db_collector_registered
    if not _db_collector_registered:
        REGISTRY.register(DB_COLLECTOR)
        _db_collector_registered = True


_api_registry: CollectorRegistry | None = None


def api_registry() -> CollectorRegistry:
    """Registry served by the API's /metrics.

    Single process: the default REGISTRY. With PROMETHEUS_MULTIPROC_DIR (uvicorn --workers N), counters of
    all workers are aggregated and the DB gauges are added once.
    """
    global _api_registry
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return REGISTRY
    if _api_registry is None:
        from prometheus_client import multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        registry.register(DB_COLLECTOR)
        _api_registry = registry
    return _api_registry


def start_metrics_server(port: int) -> bool:
    """Expose metrics of the current process (or of all Celery child processes in multiprocess mode)."""
    if port <= 0:
        return False
    from prometheus_client import start_http_server

    registry = REGISTRY
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
    try:
        start_http_server(port, registry=registry)
        logger.info("metrics server listening", extra={"port": port})
        return True
    except OSError as exc:
        logger.warning("could not start metrics server on %s: %s", port, exc)
        return False
