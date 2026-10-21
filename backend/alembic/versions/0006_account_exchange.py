"""account exchange (adapter id per account)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-21 18:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing accounts are Binance USDT-M Futures (the only exchange before this revision).
    op.add_column(
        "accounts",
        sa.Column("exchange", sa.String(length=32), server_default="binance_usdm", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("accounts", "exchange")
