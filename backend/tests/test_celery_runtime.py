"""Celery async runtime (persistent loop per worker process) and queue routing."""

import asyncio

import pytest
from sqlalchemy import text

from app.tasks import runtime
from app.tasks.celery_app import celery_app


@pytest.fixture(autouse=True)
def _fresh_runtime():
    runtime.shutdown_runtime()
    yield
    runtime.shutdown_runtime()


def test_loop_engine_and_http_pool_are_reused_across_tasks() -> None:
    from app.services.binance_http import shared_client

    async def job(sessions):
        async with sessions() as db:
            assert (await db.execute(text("select 1"))).scalar() == 1
        return asyncio.get_running_loop(), sessions, shared_client("https://example.invalid")

    loop1, sessions1, http1 = runtime.run_async(job)
    loop2, sessions2, http2 = runtime.run_async(job)
    assert loop1 is loop2 and sessions1 is sessions2 and http1 is http2
    assert not loop1.is_closed()


def test_leftover_tasks_are_cancelled_like_asyncio_run() -> None:
    started: list[asyncio.Task] = []

    async def job(_sessions):
        started.append(asyncio.get_running_loop().create_task(asyncio.sleep(3600)))
        return "done"

    assert runtime.run_async(job) == "done"
    assert started[0].cancelled()


def test_job_exception_keeps_runtime_but_propagates() -> None:
    async def boom(_sessions):
        raise ValueError("x")

    async def loop_of(_sessions):
        return asyncio.get_running_loop()

    first = runtime.run_async(loop_of)
    with pytest.raises(ValueError):
        runtime.run_async(boom)
    assert runtime.run_async(loop_of) is first


def test_shutdown_closes_loop_and_next_task_gets_a_new_one() -> None:
    async def loop_of(_sessions):
        return asyncio.get_running_loop()

    first = runtime.run_async(loop_of)
    runtime.shutdown_runtime()
    assert first.is_closed()
    assert runtime.run_async(loop_of) is not first


def test_trading_tasks_are_isolated_from_slow_queues() -> None:
    import app.tasks.alert_tasks  # noqa: F401
    import app.tasks.bot_tasks  # noqa: F401
    import app.tasks.news_tasks  # noqa: F401
    import app.tasks.scanner_tasks  # noqa: F401
    import app.tasks.trading_tasks  # noqa: F401

    def queue(name: str) -> str:
        route = celery_app.amqp.router.route({}, name)
        return route["queue"].name

    for name in ("bots.tick", "bots.tick_all", "bots.start", "paper.process_orders", "accounts.reconcile",
                 "accounts.snapshot_all", "risk.daily_reset"):  # fmt: skip
        assert queue(name) == "trading", name
    for name in ("news.ingest", "scanner.run", "calendar.refresh", "calendar.alerts", "alerts.process",
                 "alerts.deliver", "alerts.daily_summary"):  # fmt: skip
        assert queue(name) == "default", name
    # a worker started without -Q consumes both queues
    assert {q.name for q in celery_app.conf.task_queues} == {"trading", "default"}
    # distinct bindings: a message must land in exactly one queue
    assert {(q.exchange.name, q.routing_key) for q in celery_app.conf.task_queues} == {
        ("trading", "trading"),
        ("default", "default"),
    }
    assert celery_app.conf.task_ignore_result is True


def test_tick_all_dispatches_ticks_that_expire(monkeypatch) -> None:
    import uuid

    from app.tasks import bot_tasks

    ids = [uuid.uuid4(), uuid.uuid4()]
    sent: list[dict] = []
    monkeypatch.setattr(bot_tasks, "run_async", lambda fn: ids)
    monkeypatch.setattr(bot_tasks.tick_bot, "apply_async", lambda args, **kw: sent.append({"args": args, **kw}))
    assert bot_tasks.tick_all.run() == {"dispatched": 2}
    assert [s["args"] for s in sent] == [(str(i),) for i in ids]
    assert all(s["expires"] >= 5 for s in sent)
