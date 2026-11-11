"""split empties from full bottles on sessions and transfers

Revision ID: a7c41e9b2d05
Revises: 533fdcecfeff
Create Date: 2026-11-09 12:00:00.000000

Spec: docs/superpowers/specs/2026-11-09-bottle-aware-auto-assign-and-transfer-split-design.md.
Plan: docs/superpowers/plans/2026-11-09-bottle-aware-auto-assign-and-transfer-split.md, Task 1.

Added (all NOT NULL DEFAULT 0 unless noted, so existing rows read as "no empties moved"):
  * driver_bottle_sessions.empties_transferred_out / empties_transferred_in
  * driver_bottle_transfers.declared_empty_quantity
  * driver_bottle_transfers.confirmed_empty_quantity   nullable, NULL until the receiver confirms

Rollback strategy:
    downgrade() drops the four columns. LOSSY only for rows written after the upgrade that moved empties: the counts
    are discarded and the sessions' discrepancy would no longer reflect them. Stop the app services first, then
    downgrade, then put the previous code back.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a7c41e9b2d05"
down_revision = "533fdcecfeff"
branch_labels = None
depends_on = None


def upgrade():
    for column in ("empties_transferred_out", "empties_transferred_in"):
        op.add_column(
            "driver_bottle_sessions",
            sa.Column(column, sa.Integer(), nullable=False, server_default=sa.text("0")),
        )
    op.add_column(
        "driver_bottle_transfers",
        sa.Column("declared_empty_quantity", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("driver_bottle_transfers", sa.Column("confirmed_empty_quantity", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("driver_bottle_transfers", "confirmed_empty_quantity")
    op.drop_column("driver_bottle_transfers", "declared_empty_quantity")
    op.drop_column("driver_bottle_sessions", "empties_transferred_in")
    op.drop_column("driver_bottle_sessions", "empties_transferred_out")
