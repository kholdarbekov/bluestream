"""loyalty: admin-configurable surprise reward params

Revision ID: d1f7a2c5e8b0
Revises: c3f5a9d1e2b4
Create Date: 2026-06-19

Surprise rewards become fully admin-configurable on LoyaltyProgram, replacing the
hardcoded 5% / [50,100,200] / EARNED logic. server_default values reproduce the
previous behaviour for the existing default program (plus the new 7-day per-user
cooldown and global cap of 5/day). All NOT NULL via server_default so existing
rows are valid without a backfill pass.
"""

from alembic import op
import sqlalchemy as sa

revision = "d1f7a2c5e8b0"
down_revision = "c3f5a9d1e2b4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("loyalty_programs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("surprise_enabled", sa.Boolean(), nullable=False, server_default="true"))
        batch_op.add_column(sa.Column("surprise_chance_percent", sa.Integer(), nullable=False, server_default="5"))
        batch_op.add_column(
            sa.Column("surprise_amounts", sa.String(length=100), nullable=False, server_default="50,100,200")
        )
        batch_op.add_column(sa.Column("surprise_cooldown_days", sa.Integer(), nullable=False, server_default="7"))
        batch_op.add_column(sa.Column("surprise_daily_cap", sa.Integer(), nullable=False, server_default="5"))


def downgrade():
    with op.batch_alter_table("loyalty_programs", schema=None) as batch_op:
        batch_op.drop_column("surprise_daily_cap")
        batch_op.drop_column("surprise_cooldown_days")
        batch_op.drop_column("surprise_amounts")
        batch_op.drop_column("surprise_chance_percent")
        batch_op.drop_column("surprise_enabled")
