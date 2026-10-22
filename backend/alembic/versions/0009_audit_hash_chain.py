"""tamper-evident audit trail: hash chain columns, no cascading FKs, immutability trigger (PostgreSQL)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-22 13:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FKS = (
    ("fk_audit_logs_account_id_accounts", "account_id", "accounts", "SET NULL"),
    ("fk_audit_logs_user_id_users", "user_id", "users", "SET NULL"),
    ("fk_audit_logs_workspace_id_workspaces", "workspace_id", "workspaces", "CASCADE"),
)


def upgrade() -> None:
    with op.batch_alter_table("audit_logs") as batch:
        batch.add_column(sa.Column("chain_seq", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("prev_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("row_hash", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_audit_logs_chain_seq", ["chain_seq"])
        # A cascading delete / SET NULL would rewrite audited rows and break the chain.
        for name, *_ in _FKS:
            batch.drop_constraint(name, type_="foreignkey")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION audit_logs_immutable() RETURNS trigger AS $$
            BEGIN
                IF OLD.row_hash IS NOT NULL THEN
                    RAISE EXCEPTION 'audit_logs: sealed rows are immutable (id=%)', OLD.id;
                END IF;
                RETURN NEW;
            END
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            "CREATE TRIGGER audit_logs_immutable BEFORE UPDATE ON audit_logs "
            "FOR EACH ROW EXECUTE FUNCTION audit_logs_immutable()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_logs_immutable ON audit_logs")
        op.execute("DROP FUNCTION IF EXISTS audit_logs_immutable()")
        # rows pointing at deleted parents would block the FKs: clear dangling references first
        for _name, column, table, _ in _FKS:
            op.execute(
                f"DELETE FROM audit_logs WHERE {column} IS NOT NULL AND {column} NOT IN (SELECT id FROM {table})"
                if table == "workspaces"
                else f"UPDATE audit_logs SET {column} = NULL WHERE {column} IS NOT NULL "
                f"AND {column} NOT IN (SELECT id FROM {table})"
            )
    with op.batch_alter_table("audit_logs") as batch:
        for name, column, table, ondelete in _FKS:
            batch.create_foreign_key(name, table, [column], ["id"], ondelete=ondelete)
        batch.drop_constraint("uq_audit_logs_chain_seq", type_="unique")
        batch.drop_column("row_hash")
        batch.drop_column("prev_hash")
        batch.drop_column("chain_seq")
