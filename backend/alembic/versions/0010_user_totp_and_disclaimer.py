"""TOTP 2FA columns and disclaimer acknowledgement on users

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-22 13:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    "totp_enabled",
    "totp_secret_enc",
    "totp_pending_secret_enc",
    "totp_enabled_at",
    "totp_last_step",
    "totp_recovery_codes",
    "disclaimer_version",
    "disclaimer_accepted_at",
)


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("totp_enabled", sa.Boolean(), server_default=sa.false(), nullable=False))
        batch.add_column(sa.Column("totp_secret_enc", sa.Text(), nullable=True))
        batch.add_column(sa.Column("totp_pending_secret_enc", sa.Text(), nullable=True))
        batch.add_column(sa.Column("totp_enabled_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("totp_last_step", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("totp_recovery_codes", sa.JSON(), server_default="[]", nullable=False))
        batch.add_column(sa.Column("disclaimer_version", sa.String(20), nullable=True))
        batch.add_column(sa.Column("disclaimer_accepted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        for name in reversed(_COLUMNS):
            batch.drop_column(name)
