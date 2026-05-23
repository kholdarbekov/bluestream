"""Add 'partial' to driver_cash_session_status enum

Revision ID: 7e8d6c5b4a39
Revises: b9f3d2a4c6e8
Create Date: 2026-05-23 15:00:00.000000

Adds the 'partial' value to the driver_cash_session_status PostgreSQL enum so
the next migration can use it in WHERE clauses and the service layer can
assign it to sessions. Split into its own revision because PostgreSQL requires
ALTER TYPE ... ADD VALUE to be committed before the new value is usable in the
same transaction as other DDL/DML.
"""

from alembic import op


revision = "7e8d6c5b4a39"
down_revision = "b9f3d2a4c6e8"
branch_labels = None
depends_on = None


def upgrade():
    # PostgreSQL forbids using a new enum value inside the same transaction
    # that created it. The next migration references 'partial' in a partial
    # index WHERE clause, so we have to commit the ADD VALUE first via an
    # autocommit block — `ALTER TYPE ... ADD VALUE` is itself transaction-safe.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE driver_cash_session_status ADD VALUE IF NOT EXISTS 'partial'")


def downgrade():
    # PostgreSQL does not support removing individual enum values.
    # Recreating the type is not safe in production with live referencing data.
    pass
