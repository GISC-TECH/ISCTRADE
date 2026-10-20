"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-20 18:51:41.519902

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_system_settings")),
    )
    op.create_table(
        "users",
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column(
            "role", sa.Enum("admin", "trader", "viewer", name="role", native_enum=False, length=20), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("token_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("active_workspace_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)
    op.create_table(
        "workspaces",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_workspaces_owner_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
    )
    op.create_index(op.f("ix_workspaces_owner_id"), "workspaces", ["owner_id"], unique=False)
    # users <-> workspaces is a cycle: add this FK after both tables exist.
    op.create_foreign_key(
        op.f("fk_users_active_workspace_id_workspaces"),
        "users",
        "workspaces",
        ["active_workspace_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "accounts",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("encrypted_api_secret", sa.Text(), nullable=False),
        sa.Column("api_key_last4", sa.String(length=4), nullable=False),
        sa.Column("is_testnet", sa.Boolean(), nullable=False),
        sa.Column(
            "execution_mode",
            sa.Enum("paper", "live", name="executionmode", native_enum=False, length=20),
            server_default="paper",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "stopped", "running", "paused", "killed", "error", name="accountstatus", native_enum=False, length=20
            ),
            nullable=False,
        ),
        sa.Column("status_reason", sa.String(length=255), nullable=True),
        sa.Column("max_leverage", sa.Integer(), nullable=False),
        sa.Column("max_risk_per_trade_pct", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("max_daily_loss_pct", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("max_drawdown_pct", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("paper_initial_balance", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("paper_balance", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("peak_equity", sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column("day_start_equity", sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column("day_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_equity", sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_accounts_workspace_id_workspaces"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
    )
    op.create_index(op.f("ix_accounts_workspace_id"), "accounts", ["workspace_id"], unique=False)
    op.create_table(
        "backtest_runs",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("strategy_key", sa.String(length=50), nullable=False),
        sa.Column("symbol", sa.String(length=30), nullable=False),
        sa.Column("interval", sa.String(length=10), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "running", "completed", "failed", name="backteststatus", native_enum=False, length=20),
            nullable=False,
        ),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("equity_curve", sa.JSON(), nullable=False),
        sa.Column("trades", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_backtest_runs_user_id_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_backtest_runs_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_runs")),
    )
    op.create_index(op.f("ix_backtest_runs_workspace_id"), "backtest_runs", ["workspace_id"], unique=False)
    op.create_table(
        "workspace_members",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role", sa.Enum("admin", "trader", "viewer", name="role", native_enum=False, length=20), nullable=False
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_workspace_members_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_members_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_members")),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
    )
    op.create_index(op.f("ix_workspace_members_user_id"), "workspace_members", ["user_id"], unique=False)
    op.create_index(op.f("ix_workspace_members_workspace_id"), "workspace_members", ["workspace_id"], unique=False)
    op.create_table(
        "audit_logs",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event", sa.String(length=80), nullable=False),
        sa.Column("level", sa.String(length=10), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_audit_logs_account_id_accounts"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_audit_logs_user_id_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_audit_logs_workspace_id_workspaces"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_index(op.f("ix_audit_logs_created_at"), "audit_logs", ["created_at"], unique=False)
    op.create_index(op.f("ix_audit_logs_event"), "audit_logs", ["event"], unique=False)
    op.create_index(op.f("ix_audit_logs_user_id"), "audit_logs", ["user_id"], unique=False)
    op.create_index("ix_audit_workspace_created", "audit_logs", ["workspace_id", "created_at"], unique=False)
    op.create_table(
        "bot_configs",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("strategy_key", sa.String(length=50), nullable=False),
        sa.Column("symbol", sa.String(length=30), nullable=False),
        sa.Column("interval", sa.String(length=10), nullable=False),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "idle",
                "starting",
                "running",
                "paused",
                "stopping",
                "stopped",
                "error",
                "killed",
                name="botstate",
                native_enum=False,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("runtime", sa.JSON(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("last_tick_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_bot_configs_account_id_accounts"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_bot_configs_workspace_id_workspaces"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_configs")),
    )
    op.create_index(op.f("ix_bot_configs_account_id"), "bot_configs", ["account_id"], unique=False)
    op.create_index(op.f("ix_bot_configs_state"), "bot_configs", ["state"], unique=False)
    op.create_index(op.f("ix_bot_configs_workspace_id"), "bot_configs", ["workspace_id"], unique=False)
    op.create_table(
        "equity_snapshots",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("wallet_balance", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("equity", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("is_paper", sa.Boolean(), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_equity_snapshots_account_id_accounts"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_equity_snapshots_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_equity_snapshots")),
    )
    op.create_index("ix_equity_account_taken", "equity_snapshots", ["account_id", "taken_at"], unique=False)
    op.create_index(op.f("ix_equity_snapshots_account_id"), "equity_snapshots", ["account_id"], unique=False)
    op.create_index(op.f("ix_equity_snapshots_workspace_id"), "equity_snapshots", ["workspace_id"], unique=False)
    op.create_table(
        "positions",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=30), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("mark_price", sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column("unrealized_pnl", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("stop_loss", sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column("is_paper", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_positions_account_id_accounts"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_positions_workspace_id_workspaces"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_positions")),
        sa.UniqueConstraint("account_id", "symbol", name="uq_positions_account_symbol"),
    )
    op.create_index(op.f("ix_positions_account_id"), "positions", ["account_id"], unique=False)
    op.create_index(op.f("ix_positions_workspace_id"), "positions", ["workspace_id"], unique=False)
    op.create_table(
        "orders",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=True),
        sa.Column("symbol", sa.String(length=30), nullable=False),
        sa.Column("side", sa.Enum("BUY", "SELL", name="orderside", native_enum=False), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "MARKET", "LIMIT", "STOP_MARKET", "TAKE_PROFIT_MARKET", name="ordertype", native_enum=False, length=30
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "NEW",
                "PARTIALLY_FILLED",
                "FILLED",
                "CANCELED",
                "REJECTED",
                "EXPIRED",
                name="orderstatus",
                native_enum=False,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("quantity", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("price", sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column("stop_price", sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column("stop_loss", sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("reduce_only", sa.Boolean(), nullable=False),
        sa.Column("executed_qty", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("avg_price", sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column("client_order_id", sa.String(length=36), nullable=False),
        sa.Column("exchange_order_id", sa.String(length=40), nullable=True),
        sa.Column("is_paper", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_orders_account_id_accounts"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["bot_id"], ["bot_configs.id"], name=op.f("fk_orders_bot_id_bot_configs"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_orders_workspace_id_workspaces"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        sa.UniqueConstraint("client_order_id", name=op.f("uq_orders_client_order_id")),
    )
    op.create_index("ix_orders_account_created", "orders", ["account_id", "created_at"], unique=False)
    op.create_index(op.f("ix_orders_account_id"), "orders", ["account_id"], unique=False)
    op.create_index(op.f("ix_orders_bot_id"), "orders", ["bot_id"], unique=False)
    op.create_index(op.f("ix_orders_exchange_order_id"), "orders", ["exchange_order_id"], unique=False)
    op.create_index(op.f("ix_orders_symbol"), "orders", ["symbol"], unique=False)
    op.create_index(op.f("ix_orders_workspace_id"), "orders", ["workspace_id"], unique=False)
    op.create_table(
        "trades",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("bot_id", sa.Uuid(), nullable=True),
        sa.Column("symbol", sa.String(length=30), nullable=False),
        sa.Column("side", sa.Enum("BUY", "SELL", name="orderside", native_enum=False), nullable=False),
        sa.Column("price", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("fee", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("fee_asset", sa.String(length=10), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("exchange_trade_id", sa.String(length=40), nullable=True),
        sa.Column("is_paper", sa.Boolean(), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_trades_account_id_accounts"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["bot_id"], ["bot_configs.id"], name=op.f("fk_trades_bot_id_bot_configs"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_trades_order_id_orders"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_trades_workspace_id_workspaces"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trades")),
    )
    op.create_index("ix_trades_account_executed", "trades", ["account_id", "executed_at"], unique=False)
    op.create_index(op.f("ix_trades_account_id"), "trades", ["account_id"], unique=False)
    op.create_index(op.f("ix_trades_order_id"), "trades", ["order_id"], unique=False)
    op.create_index(op.f("ix_trades_symbol"), "trades", ["symbol"], unique=False)
    op.create_index(op.f("ix_trades_workspace_id"), "trades", ["workspace_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_trades_workspace_id"), table_name="trades")
    op.drop_index(op.f("ix_trades_symbol"), table_name="trades")
    op.drop_index(op.f("ix_trades_order_id"), table_name="trades")
    op.drop_index(op.f("ix_trades_account_id"), table_name="trades")
    op.drop_index("ix_trades_account_executed", table_name="trades")
    op.drop_table("trades")
    op.drop_index(op.f("ix_orders_workspace_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_symbol"), table_name="orders")
    op.drop_index(op.f("ix_orders_exchange_order_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_bot_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_account_id"), table_name="orders")
    op.drop_index("ix_orders_account_created", table_name="orders")
    op.drop_table("orders")
    op.drop_index(op.f("ix_positions_workspace_id"), table_name="positions")
    op.drop_index(op.f("ix_positions_account_id"), table_name="positions")
    op.drop_table("positions")
    op.drop_index(op.f("ix_equity_snapshots_workspace_id"), table_name="equity_snapshots")
    op.drop_index(op.f("ix_equity_snapshots_account_id"), table_name="equity_snapshots")
    op.drop_index("ix_equity_account_taken", table_name="equity_snapshots")
    op.drop_table("equity_snapshots")
    op.drop_index(op.f("ix_bot_configs_workspace_id"), table_name="bot_configs")
    op.drop_index(op.f("ix_bot_configs_state"), table_name="bot_configs")
    op.drop_index(op.f("ix_bot_configs_account_id"), table_name="bot_configs")
    op.drop_table("bot_configs")
    op.drop_index("ix_audit_workspace_created", table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_user_id"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_event"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_created_at"), table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index(op.f("ix_workspace_members_workspace_id"), table_name="workspace_members")
    op.drop_index(op.f("ix_workspace_members_user_id"), table_name="workspace_members")
    op.drop_table("workspace_members")
    op.drop_index(op.f("ix_backtest_runs_workspace_id"), table_name="backtest_runs")
    op.drop_table("backtest_runs")
    op.drop_index(op.f("ix_accounts_workspace_id"), table_name="accounts")
    op.drop_table("accounts")
    op.drop_constraint(op.f("fk_users_active_workspace_id_workspaces"), "users", type_="foreignkey")
    op.drop_index(op.f("ix_workspaces_owner_id"), table_name="workspaces")
    op.drop_table("workspaces")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
    op.drop_table("system_settings")
