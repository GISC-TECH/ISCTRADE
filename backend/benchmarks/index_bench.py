"""Index benchmark for migration 0007 on a real PostgreSQL (seeded with generate_series).

    BENCH_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/scratch_db python -m benchmarks.index_bench

Runs ``alembic downgrade 0006``, seeds ~400k orders / 400k trades / 200k audit rows, measures the hot queries
(EXPLAIN ANALYZE execution time, median of 15), then ``alembic upgrade head`` and measures again.
The database is wiped: use a dedicated scratch database.
"""

from __future__ import annotations

import asyncio
import os
import re
import statistics
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg

URL = os.environ["BENCH_DATABASE_URL"]
DSN = URL.replace("+asyncpg", "")
BACKEND = Path(__file__).resolve().parents[1]
N_ORDERS = int(os.environ.get("BENCH_ROWS", "400000"))

QUERIES = {
    "history orders (workspace, newest 50)": (
        "SELECT * FROM orders WHERE workspace_id = $1 ORDER BY created_at DESC LIMIT 50", "ws"),
    "history orders count (workspace)": ("SELECT count(*) FROM orders WHERE workspace_id = $1", "ws"),
    "paper resting LIMIT orders (account)": (
        "SELECT * FROM orders WHERE account_id = $1 AND is_paper AND status = 'NEW' AND type = 'LIMIT'", "acc"),
    "reconciler fill dedup (account, trade id)": (
        "SELECT id FROM trades WHERE account_id = $1 AND exchange_trade_id = $2", "acc_trade"),
    "history trades (workspace, newest 50)": (
        "SELECT * FROM trades WHERE workspace_id = $1 ORDER BY executed_at DESC LIMIT 50", "ws"),
    "bot delete: trades SET bot_id NULL": ("UPDATE trades SET bot_id = NULL WHERE bot_id = $1", "bot"),
    "account delete: audit SET account_id NULL": (
        "UPDATE audit_logs SET account_id = NULL WHERE account_id = $1", "acc"),
}  # fmt: skip


def alembic(*args: str) -> None:
    env = {**os.environ, "DATABASE_URL": URL}
    res = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env, capture_output=True, text=True)
    if res.returncode:
        raise SystemExit(res.stdout + res.stderr)


async def seed(conn: asyncpg.Connection) -> dict:
    user = uuid.uuid4()
    await conn.execute(
        "INSERT INTO users (id, email, hashed_password, full_name, role, is_active, token_version, created_at, "
        "updated_at) VALUES ($1, 'bench@example.com', 'x', 'b', 'admin', true, 0, now(), now())",
        user,
    )
    accounts, workspaces = [], []
    for w in range(10):
        ws = uuid.uuid4()
        workspaces.append(ws)
        await conn.execute(
            "INSERT INTO workspaces (id, name, owner_id, created_at, updated_at) VALUES ($1, $2, $3, now(), now())",
            ws, f"ws{w}", user,
        )  # fmt: skip
        for a in range(5):
            acc = uuid.uuid4()
            accounts.append((acc, ws))
            await conn.execute(
                "INSERT INTO accounts (id, workspace_id, label, exchange, encrypted_api_key, encrypted_api_secret, "
                "api_key_last4, is_testnet, execution_mode, status, paper_balance, paper_initial_balance, "
                "max_risk_per_trade_pct, max_leverage, max_daily_loss_pct, max_drawdown_pct, created_at, updated_at) "
                "VALUES ($1, $2, $3, 'binance_usdm', 'x', 'x', '0000', true, 'paper', 'running', 10000, 10000, 1, 5, "
                "3, 15, now(), now())",
                acc, ws, f"acc{a}",
            )  # fmt: skip
    bot = uuid.uuid4()
    acc0, ws0 = accounts[0]
    await conn.execute(
        "INSERT INTO bot_configs (id, workspace_id, account_id, name, strategy_key, symbol, interval, params, "
        "leverage, state, is_enabled, runtime, created_at, updated_at) VALUES ($1, $2, $3, 'b', 'grid', 'BTCUSDT', "
        "'1h', '{}', 1, 'stopped', true, '{}', now(), now())",
        bot, ws0, acc0,
    )  # fmt: skip
    acc_ids = [a for a, _ in accounts]
    ws_of = [w for _, w in accounts]
    await conn.execute(
        """
        INSERT INTO orders (id, workspace_id, account_id, bot_id, symbol, side, type, status, quantity, leverage,
                            reduce_only, executed_qty, client_order_id, is_paper, created_at, updated_at)
        SELECT gen_random_uuid(), ($2::uuid[])[1 + i % 50], ($1::uuid[])[1 + i % 50], NULL, 'S' || (i % 40) || 'USDT',
               CASE WHEN i % 2 = 0 THEN 'BUY' ELSE 'SELL' END,
               CASE WHEN i % 10 = 0 THEN 'LIMIT' ELSE 'MARKET' END,
               CASE WHEN i % 1000 = 0 THEN 'NEW' WHEN i % 7 = 0 THEN 'REJECTED' ELSE 'FILLED' END,
               1, 1, false, 1, 'btp-' || i, true, now() - (i || ' seconds')::interval, now()
        FROM generate_series(1, $3) AS i
        """,
        acc_ids, ws_of, N_ORDERS,
    )  # fmt: skip
    await conn.execute(
        """
        INSERT INTO trades (id, workspace_id, account_id, bot_id, symbol, side, price, quantity, fee, fee_asset,
                            realized_pnl, exchange_trade_id, is_paper, executed_at)
        SELECT gen_random_uuid(), ($2::uuid[])[1 + i % 50], ($1::uuid[])[1 + i % 50],
               CASE WHEN i % 5000 = 0 THEN $4::uuid END, 'S' || (i % 40) || 'USDT', 'BUY', 100, 1, 0.04, 'USDT', 0,
               i::text, false, now() - (i || ' seconds')::interval
        FROM generate_series(1, $3) AS i
        """,
        acc_ids, ws_of, N_ORDERS, bot,
    )  # fmt: skip
    await conn.execute(
        """
        INSERT INTO audit_logs (id, created_at, event, level, workspace_id, account_id, data)
        SELECT gen_random_uuid(), now() - (i || ' seconds')::interval, 'order.filled', 'info',
               ($2::uuid[])[1 + i % 50], ($1::uuid[])[1 + i % 50], '{}'
        FROM generate_series(1, $3 / 2) AS i
        """,
        acc_ids, ws_of, N_ORDERS,
    )  # fmt: skip
    await conn.execute("ANALYZE")
    # a "victim" account with a few audit rows for the delete scenario
    return {"ws": (ws0,), "acc": (acc0,), "acc_trade": (acc_ids[7], str(N_ORDERS // 2 + 7)), "bot": (bot,)}


async def measure(conn: asyncpg.Connection, params: dict) -> dict[str, float]:
    out = {}
    for name, (sql, key) in QUERIES.items():
        times = []
        for _ in range(15):
            tr = conn.transaction()
            await tr.start()
            plan = await conn.fetch("EXPLAIN (ANALYZE, FORMAT TEXT) " + sql, *params[key])
            await tr.rollback()
            text = "\n".join(r[0] for r in plan)
            times.append(float(re.search(r"Execution Time: ([\d.]+) ms", text).group(1)))
        out[name] = statistics.median(times)
    return out


async def main() -> None:
    alembic("upgrade", "head")
    alembic("downgrade", "0006")
    conn = await asyncpg.connect(DSN)
    try:
        for table in ("audit_logs", "trades", "orders", "bot_configs", "accounts", "workspaces", "users"):
            await conn.execute(f"TRUNCATE {table} CASCADE")
        params = await seed(conn)
        before = await measure(conn, params)
    finally:
        await conn.close()
    alembic("upgrade", "head")
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("ANALYZE")
        after = await measure(conn, params)
    finally:
        await conn.close()
    print(f"rows: orders={N_ORDERS} trades={N_ORDERS} audit_logs={N_ORDERS // 2}")
    print(f"{'query':<46} {'0006 (ms)':>10} {'0007 (ms)':>10}")
    for name in QUERIES:
        print(f"{name:<46} {before[name]:>10.3f} {after[name]:>10.3f}")


if __name__ == "__main__":
    asyncio.run(main())
