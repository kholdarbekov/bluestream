"""marking code task config + run ledger + per-product overrides

Adds:
* ``marking_code_task_config`` — singleton row (id=1) with schedule + tuning knobs.
* ``marking_code_task_runs`` — execution ledger for parent fan-out + per-product
  replenish tasks.
* ``override_*`` nullable columns on ``product_fiscal_profiles`` for per-product
  tuning overrides.

The singleton row is seeded with the current env-var defaults so behaviour
does not change at deploy time.

Revision ID: 4d2b1e6a5f80
Revises: 7e2a4f1c9b5d
Create Date: 2026-05-18 14:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "4d2b1e6a5f80"
down_revision = "7e2a4f1c9b5d"
branch_labels = None
depends_on = None


SCHEDULE_TYPE_NAME = "marking_code_schedule_type"
RUN_STATUS_NAME = "marking_code_run_status"


def upgrade():
    bind = op.get_bind()

    # Pre-create enums with create_type=False so the column definitions below
    # don't try to auto-create them again (which would generate an empty
    # CREATE TYPE ... AS ENUM () statement and fail).
    schedule_type = postgresql.ENUM(
        "daily",
        "weekly",
        "interval_days",
        name=SCHEDULE_TYPE_NAME,
        create_type=False,
    )
    run_status = postgresql.ENUM(
        "running",
        "success",
        "failed",
        "skipped",
        name=RUN_STATUS_NAME,
        create_type=False,
    )
    schedule_type.create(bind, checkfirst=True)
    run_status.create(bind, checkfirst=True)

    # ------------------------------------------------------------------
    # 1. Singleton config table
    # ------------------------------------------------------------------
    op.create_table(
        "marking_code_task_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("schedule_type", schedule_type, nullable=False, server_default="daily"),
        sa.Column("interval_days", sa.Integer(), nullable=True),
        sa.Column("day_of_week", sa.SmallInteger(), nullable=True),
        sa.Column("execution_hour", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("execution_minute", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("schedule_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("target_min", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("target_max", sa.Integer(), nullable=False, server_default="500"),
        sa.Column("trend_window_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("runway_days", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "safety_multiplier",
            sa.Numeric(precision=5, scale=2),
            nullable=False,
            server_default="1.50",
        ),
        sa.Column(
            "low_water_ratio",
            sa.Numeric(precision=4, scale=3),
            nullable=False,
            server_default="0.250",
        ),
        sa.Column(
            "asl_belgisi_utilisation_api_chunk_size",
            sa.Integer(),
            nullable=False,
            server_default="200",
        ),
        sa.Column(
            "tc_utilisation_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "tc_utilisation_delay_seconds",
            sa.Integer(),
            nullable=False,
            server_default="120",
        ),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # Seed the singleton row.
    op.execute(
        sa.text(
            """
            INSERT INTO marking_code_task_config (
                id, schedule_type, execution_hour, execution_minute, schedule_version,
                target_min, target_max, trend_window_days, runway_days,
                safety_multiplier, low_water_ratio, asl_belgisi_utilisation_api_chunk_size,
                tc_utilisation_enabled, tc_utilisation_delay_seconds,
                created_at, updated_at
            ) VALUES (
                1, 'daily', 0, 0, 1,
                5, 500, 7, 1,
                1.50, 0.250, 200,
                true, 120,
                NOW(), NOW()
            )
            ON CONFLICT (id) DO NOTHING;
            """
        )
    )

    # ------------------------------------------------------------------
    # 2. Task-run ledger
    # ------------------------------------------------------------------
    op.create_table(
        "marking_code_task_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_name", sa.String(length=120), nullable=False, index=True),
        sa.Column("run_kind", sa.String(length=32), nullable=False, server_default="daily"),
        sa.Column(
            "parent_run_id",
            sa.Integer(),
            sa.ForeignKey("marking_code_task_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", run_status, nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("requested", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("utilised", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_invalid", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pre_utilised_before", sa.Integer(), nullable=True),
        sa.Column("pre_utilised_after", sa.Integer(), nullable=True),
        sa.Column("target_value", sa.Integer(), nullable=True),
        sa.Column("result_summary", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("triggered_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "idx_mc_task_runs_task_started",
        "marking_code_task_runs",
        ["task_name", "started_at"],
    )
    op.create_index(
        "idx_mc_task_runs_product_started",
        "marking_code_task_runs",
        ["product_id", "started_at"],
    )
    op.create_index(
        "idx_mc_task_runs_parent",
        "marking_code_task_runs",
        ["parent_run_id"],
    )
    op.create_index(
        "idx_mc_task_runs_status_started",
        "marking_code_task_runs",
        ["status", "started_at"],
    )

    # ------------------------------------------------------------------
    # 3. Per-product override columns on product_fiscal_profiles
    # ------------------------------------------------------------------
    with op.batch_alter_table("product_fiscal_profiles") as batch_op:
        batch_op.add_column(sa.Column("override_target_min", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("override_target_max", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("override_trend_window_days", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("override_runway_days", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("override_safety_multiplier", sa.Numeric(precision=5, scale=2), nullable=True))
        batch_op.add_column(sa.Column("override_low_water_ratio", sa.Numeric(precision=4, scale=3), nullable=True))
        batch_op.add_column(sa.Column("override_asl_belgisi_utilisation_api_chunk_size", sa.Integer(), nullable=True))


def downgrade():
    bind = op.get_bind()

    with op.batch_alter_table("product_fiscal_profiles") as batch_op:
        batch_op.drop_column("override_asl_belgisi_utilisation_api_chunk_size")
        batch_op.drop_column("override_low_water_ratio")
        batch_op.drop_column("override_safety_multiplier")
        batch_op.drop_column("override_runway_days")
        batch_op.drop_column("override_trend_window_days")
        batch_op.drop_column("override_target_max")
        batch_op.drop_column("override_target_min")

    op.drop_index("idx_mc_task_runs_status_started", table_name="marking_code_task_runs")
    op.drop_index("idx_mc_task_runs_parent", table_name="marking_code_task_runs")
    op.drop_index("idx_mc_task_runs_product_started", table_name="marking_code_task_runs")
    op.drop_index("idx_mc_task_runs_task_started", table_name="marking_code_task_runs")
    op.drop_table("marking_code_task_runs")
    op.drop_table("marking_code_task_config")

    postgresql.ENUM(name=RUN_STATUS_NAME).drop(bind, checkfirst=True)
    postgresql.ENUM(name=SCHEDULE_TYPE_NAME).drop(bind, checkfirst=True)
