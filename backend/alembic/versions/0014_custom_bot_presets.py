"""custom bot presets per workspace

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-22 15:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bot_presets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE", name="fk_bot_presets_workspace_id_workspaces"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_bot_presets_created_by_users"),
            nullable=True,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("strategy_key", sa.String(60), nullable=False),
        sa.Column("interval", sa.String(5), nullable=False),
        sa.Column("symbol", sa.String(30), nullable=False),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("risk_level", sa.String(12), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("based_on", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_bot_presets"),
    )
    op.create_index("ix_bot_presets_workspace_name", "bot_presets", ["workspace_id", "name"])


def downgrade() -> None:
    op.drop_index("ix_bot_presets_workspace_name", table_name="bot_presets")
    op.drop_table("bot_presets")
