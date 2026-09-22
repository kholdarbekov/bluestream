"""add sales_agent_day_plans

Revision ID: d8e9f0a1b2c3
Revises: b7c8d9e0f1a2
Create Date: 2026-09-16 09:00:00.000000

Phase 3 of the Sales Agent design (docs/superpowers/specs/2026-09-07-sales-agent-role-design.md).
Hand-written so every constraint is named, following b7c8d9e0f1a2.

Tables added:
  * sales_agent_day_plans — one row per active sales agent per LOCAL day, written
                    at 01:20 by `sales.snapshot_agent_day_plans`: how many outlets
                    were due for that agent that day and how many were already
                    overdue. It exists because `outlets.next_visit_due_at` is a
                    single mutable column with no history — the 01:00 recompute
                    rewrites it for every non-lost outlet — so "how many shops were
                    due last Tuesday" is otherwise unanswerable, and plan-vs-fact is
                    a question about last week.

The spec's phasing table said phase 3 needed no migration; that was written before
the due-date column's mutability was traced. Forward-only in effect: days before
this migration deploys have no row and every read publishes `plan_source: "none"`
for them rather than inventing a denominator.

Rollback strategy:
    downgrade() drops the index and then the table. The rows are lost and CANNOT be
    recomputed — the numbers they hold were true only on the day they were taken —
    so a rollback permanently ends plan-vs-fact for the days it covered. Nothing
    else references the table: every other sales surface (the due list, the morning
    digest, the KPI metrics) reads live columns and is untouched.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "d8e9f0a1b2c3"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sales_agent_day_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "agent_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_sales_agent_day_plans_agent_user_id"),
            nullable=False,
        ),
        sa.Column("plan_date", sa.Date(), nullable=False),
        sa.Column("due_count", sa.Integer(), nullable=False),
        sa.Column("overdue_count", sa.Integer(), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("agent_user_id", "plan_date", name="uq_sales_agent_day_plans_agent_date"),
    )
    op.create_index("ix_sales_agent_day_plans_plan_date", "sales_agent_day_plans", ["plan_date"])


def downgrade():
    op.drop_index("ix_sales_agent_day_plans_plan_date", table_name="sales_agent_day_plans")
    op.drop_table("sales_agent_day_plans")
