"""account hard capital cap and transfer-history cursor (risk baselines)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-22 13:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("max_capital_usdt", sa.Numeric(28, 10), nullable=True))
    op.add_column("accounts", sa.Column("transfers_synced_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "transfers_synced_at")
    op.drop_column("accounts", "max_capital_usdt")
