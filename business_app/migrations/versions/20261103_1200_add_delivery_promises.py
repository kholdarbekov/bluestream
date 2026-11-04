"""add delivery_promises: the append-only record of every arrival promise

Revision ID: 9e7056f6124a
Revises: 3f8b2d6a9c41
Create Date: 2026-11-03 12:00:00.000000

Spec: docs/superpowers/specs/2026-11-03-honest-capacity-aware-eta-design.md §4.10 and §4.12a.
Plan: docs/superpowers/plans/2026-11-03-honest-eta-phase1.md, Task 1.

Added:
  * delivery_promises  one row per promise the engine produces for a delivery. An "in transit" tap writes
                       a SHOWN row (google_tap_v1) and a HIDDEN shadow row (osrm_shadow_v1), so
                       (delivery_status_history_id, model_version) is unique wherever the tap is known: a
                       re-run of the notification worker cannot record the same tap twice.

Both foreign keys are plain (no ON DELETE): a promise leaves only with its order, when OrderDeletionService
deletes children first. The vocabulary tuples below are FROZEN copies of
business_app/models/delivery_promise.py; tests/integration/test_delivery_promise_pg_constraints.py pins them
equal to the model's.

Rollback strategy:
    downgrade() drops the table; nothing else reads it. LOSSY: every recorded promise is discarded. Roll the
    code that records and shows promises back first, or it fails on the missing table.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "9e7056f6124a"
down_revision = "3f8b2d6a9c41"
branch_labels = None
depends_on = None

TABLE = "delivery_promises"

# Frozen copies (CONVENTIONS.md): never import the live tuples into a migration.
PROMISE_STAGES = ("waiting", "on_the_way")
PROMISE_DAY_KINDS = ("today", "tomorrow", "date", "unconfirmed")


def _quoted(values):
    return ", ".join(f"'{v}'" for v in values)


def upgrade():
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "delivery_id",
            sa.Integer(),
            sa.ForeignKey("deliveries.id", name=f"fk_{TABLE}_delivery_id"),
            nullable=False,
        ),
        sa.Column(
            "delivery_status_history_id",
            sa.Integer(),
            sa.ForeignKey("delivery_status_history.id", name=f"fk_{TABLE}_delivery_status_history_id"),
            nullable=True,
        ),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("expected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("day_kind", sa.String(12), nullable=True),
        sa.Column("stops_ahead", sa.Integer(), nullable=True),
        sa.Column("day_started", sa.Boolean(), nullable=True),
        sa.Column("drivers_working", sa.Integer(), nullable=True),
        sa.Column("model_version", sa.String(32), nullable=False),
        sa.Column("source", sa.String(40), nullable=True),
        sa.Column("shown", sa.Boolean(), nullable=False),
        sa.CheckConstraint(f"stage IN ({_quoted(PROMISE_STAGES)})", name=f"ck_{TABLE}_stage"),
        sa.CheckConstraint(
            f"day_kind IS NULL OR day_kind IN ({_quoted(PROMISE_DAY_KINDS)})", name=f"ck_{TABLE}_day_kind"
        ),
        sa.CheckConstraint(
            "window_start IS NULL OR window_end IS NULL OR window_start <= window_end",
            name=f"ck_{TABLE}_window_order",
        ),
    )
    op.create_index(f"ix_{TABLE}_delivery_id_computed_at", TABLE, ["delivery_id", "computed_at"])
    tap_known = sa.text("delivery_status_history_id IS NOT NULL")
    op.create_index(
        f"uq_{TABLE}_history_model",
        TABLE,
        ["delivery_status_history_id", "model_version"],
        unique=True,
        postgresql_where=tap_known,
        sqlite_where=tap_known,
    )


def downgrade():
    op.drop_table(TABLE)
