"""performance indexes: history listings, open orders, reconciler fill dedup, FK lookups

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-21 19:00:00.000000

Adds composite indexes for the hot queries (see docs/AUDIT.md, "Desempenho") and drops the single-column
workspace_id/account_id indexes of orders/trades/equity_snapshots that became redundant: the composite
indexes start with the same column, so FK cascades and filters still use an index, and every insert on these
write-heavy tables maintains fewer indexes.

On PostgreSQL indexes are created/dropped CONCURRENTLY (no write lock on orders/trades during deploy).
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW = [
    ("ix_orders_workspace_created", "orders", ["workspace_id", "created_at"]),
    ("ix_orders_account_status", "orders", ["account_id", "status"]),
    ("ix_trades_workspace_executed", "trades", ["workspace_id", "executed_at"]),
    ("ix_trades_account_exchange_trade", "trades", ["account_id", "exchange_trade_id"]),
    ("ix_trades_bot_id", "trades", ["bot_id"]),
    ("ix_equity_workspace_taken", "equity_snapshots", ["workspace_id", "taken_at"]),
    ("ix_audit_logs_account_id", "audit_logs", ["account_id"]),
    ("ix_alerts_account_id", "alerts", ["account_id"]),
]
REDUNDANT = [
    ("ix_orders_workspace_id", "orders", ["workspace_id"]),
    ("ix_orders_account_id", "orders", ["account_id"]),
    ("ix_trades_workspace_id", "trades", ["workspace_id"]),
    ("ix_trades_account_id", "trades", ["account_id"]),
    ("ix_equity_snapshots_workspace_id", "equity_snapshots", ["workspace_id"]),
    ("ix_equity_snapshots_account_id", "equity_snapshots", ["account_id"]),
]


def _apply(create: list, drop: list) -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for name, table, cols in create:
                op.create_index(name, table, cols, postgresql_concurrently=True, if_not_exists=True)
            for name, table, _ in drop:
                op.drop_index(name, table_name=table, postgresql_concurrently=True, if_exists=True)
        return
    for name, table, cols in create:
        op.create_index(name, table, cols)
    for name, table, _ in drop:
        op.drop_index(name, table_name=table)


def upgrade() -> None:
    _apply(NEW, REDUNDANT)


def downgrade() -> None:
    _apply(REDUNDANT, NEW)
