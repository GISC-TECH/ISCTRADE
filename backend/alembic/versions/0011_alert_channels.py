"""alert settings: Telegram chat ids and e-mail recipients per workspace

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-22 14:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("alert_settings") as batch:
        batch.add_column(sa.Column("telegram_chat_ids", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("emails", sa.JSON(), nullable=False, server_default="[]"))
    with op.batch_alter_table("alert_settings") as batch:
        batch.alter_column("telegram_chat_ids", server_default=None)
        batch.alter_column("emails", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("alert_settings") as batch:
        batch.drop_column("emails")
        batch.drop_column("telegram_chat_ids")
