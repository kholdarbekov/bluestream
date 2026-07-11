"""subscription parity cleanup

Revision ID: 3d7a1e9f6c52
Revises: f5b2c8d41a7e
Create Date: 2026-07-11 11:05:00.000000

Rollback strategy:
    downgrade() drops the ``ck_subscriptions_discount_percentage_range`` CHECK
    constraint and re-adds ``auto_payment`` (Boolean, nullable) and
    ``payment_token`` (String(255), nullable) as empty columns. Safe in both
    directions: Task 13 already removed the corresponding model attributes,
    and the live table has 0 rows, so there is no data to lose or backfill
    either way.

Context: Task 13 removed the ``auto_payment``/``payment_token`` model
attributes from ``business_app/models/subscription.py`` (dead columns —
superseded by the ``payment_method`` enum and the Click/Payme integrations,
which don't store a raw payment token on the subscription row). This
migration drops the now-orphaned DB columns and adds a defence-in-depth CHECK
constraint bounding ``discount_percentage`` to 0..100, matching the pattern
in ``b8e3c9f5d2a4_arch005_money_points_check_constraints.py`` (Pydantic
already enforces the same bound at every write path, see
``subscription_serializers.py``).

Note: ``flask db migrate`` autogenerate additionally proposed unrelated
drift from other in-flight work (NOT NULL tightening on
``loyalty_consecutive_strike_rules``/``loyalty_streak_rules`` timestamps, a
new index on ``marking_code_task_runs.status``, and unique-constraint/index
churn on ``reward_redemptions.code`` and
``support_conversations.user_id``). None of that belongs in this migration
and it was deliberately excluded — see the Task 14 report for detail.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "3d7a1e9f6c52"
down_revision = "f5b2c8d41a7e"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.drop_column("payment_token")
        batch_op.drop_column("auto_payment")
        batch_op.create_check_constraint(
            "ck_subscriptions_discount_percentage_range",
            "discount_percentage >= 0 AND discount_percentage <= 100",
        )


def downgrade():
    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.drop_constraint("ck_subscriptions_discount_percentage_range", type_="check")
        batch_op.add_column(sa.Column("auto_payment", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("payment_token", sa.String(length=255), nullable=True))
