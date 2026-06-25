"""consecutive strike bonus rules

Revision ID: e2c5a8f1b3d7
Revises: c1f2a3b4d5e6
Create Date: 2026-06-24

"""

from alembic import op
import sqlalchemy as sa

revision = "e2c5a8f1b3d7"
down_revision = "c1f2a3b4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "loyalty_consecutive_strike_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("program_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("required_consecutive", sa.Integer(), nullable=False),
        sa.Column("combine_mode", sa.String(length=8), nullable=False, server_default="all"),
        sa.Column("bonus_points", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["program_id"], ["loyalty_programs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("required_consecutive >= 1", name="ck_loyalty_consec_required_pos"),
        sa.CheckConstraint("bonus_points >= 0", name="ck_loyalty_consec_bonus_nonneg"),
        sa.CheckConstraint("combine_mode IN ('all', 'any')", name="ck_loyalty_consec_combine_mode"),
    )
    op.create_index(
        "ix_loyalty_consecutive_strike_rules_program_id",
        "loyalty_consecutive_strike_rules",
        ["program_id"],
    )
    op.create_table(
        "loyalty_consec_rule_strikes",
        sa.Column("consecutive_strike_rule_id", sa.Integer(), nullable=False),
        sa.Column("streak_rule_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["consecutive_strike_rule_id"],
            ["loyalty_consecutive_strike_rules.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["streak_rule_id"], ["loyalty_streak_rules.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("consecutive_strike_rule_id", "streak_rule_id"),
    )


def downgrade():
    op.drop_table("loyalty_consec_rule_strikes")
    op.drop_index(
        "ix_loyalty_consecutive_strike_rules_program_id",
        table_name="loyalty_consecutive_strike_rules",
    )
    op.drop_table("loyalty_consecutive_strike_rules")
