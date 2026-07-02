"""Add FORCE_CLOSED status + force_close_reason to driver cash sessions.

Revision ID: f5b2c8d41a7e
Revises: d3f7a1c9e5b2
Create Date: 2026-07-01

Adds the 'force_closed' value to the driver_cash_session_status enum and a
nullable force_close_reason text column. The enum ADD VALUE runs in an
autocommit block because Postgres cannot add an enum value inside the
migration's transaction. Downgrade drops the column only; Postgres cannot
cleanly drop an enum value, so 'force_closed' is left in place (harmless).
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "f5b2c8d41a7e"
down_revision = "d3f7a1c9e5b2"
branch_labels = None
depends_on = None


def upgrade():
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE driver_cash_session_status ADD VALUE IF NOT EXISTS 'force_closed'")
    op.add_column(
        "driver_cash_sessions",
        sa.Column("force_close_reason", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("driver_cash_sessions", "force_close_reason")
