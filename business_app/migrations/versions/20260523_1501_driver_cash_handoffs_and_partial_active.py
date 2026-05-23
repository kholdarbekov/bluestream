"""Create driver_cash_handoffs and extend active-session uniqueness to partial

Revision ID: 6f5d4c3b2a18
Revises: 7e8d6c5b4a39
Create Date: 2026-05-23 15:01:00.000000

Three things happen here, all dependent on the 'partial' enum value added in
revision 7e8d6c5b4a39:

1. Create the driver_cash_handoffs table that records every physical cash
   handoff. The session-level `declared_cash` column becomes the running sum
   of unvoided handoffs.

2. Backfill: for every existing session with a non-null declared_cash, insert
   one synthetic handoff so the invariant
       declared_cash == SUM(unvoided handoffs.amount)
   holds across legacy data.

3. Recreate the uq_driver_cash_sessions_driver_active partial unique index so
   PARTIAL sessions are also covered (a driver can only have one
   open/overdue/partial session at a time).
"""

from alembic import op
import sqlalchemy as sa


revision = "6f5d4c3b2a18"
down_revision = "7e8d6c5b4a39"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "driver_cash_handoffs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("driver_cash_session_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("recorded_by_user_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_by_user_id", sa.Integer(), nullable=True),
        sa.Column("void_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["driver_cash_session_id"], ["driver_cash_sessions.id"]),
        sa.ForeignKeyConstraint(["recorded_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["voided_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("driver_cash_handoffs", schema=None) as batch_op:
        batch_op.create_index(
            "idx_driver_cash_handoffs_session_occurred",
            ["driver_cash_session_id", "occurred_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_driver_cash_handoffs_driver_cash_session_id"),
            ["driver_cash_session_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_driver_cash_handoffs_recorded_by_user_id"),
            ["recorded_by_user_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_driver_cash_handoffs_voided_by_user_id"),
            ["voided_by_user_id"],
            unique=False,
        )

    # Backfill one handoff per legacy session that already has a declared_cash.
    # COALESCE on submitted_at keeps the chronology consistent for rows where
    # the legacy submit didn't stamp submitted_at (e.g. resolved sessions).
    op.execute(
        sa.text(
            """
            INSERT INTO driver_cash_handoffs (
                driver_cash_session_id, amount, occurred_at,
                recorded_by_user_id, notes, created_at, updated_at
            )
            SELECT
                id,
                declared_cash,
                COALESCE(submitted_at, session_ended_at, last_cash_activity_at, session_started_at, NOW()),
                submitted_by_user_id,
                'backfilled from declared_cash by 20260523_1501 migration',
                NOW(),
                NOW()
            FROM driver_cash_sessions
            WHERE declared_cash IS NOT NULL
            """
        )
    )

    # Recreate the active-session partial unique index so PARTIAL also blocks
    # parallel sessions for the same driver.
    with op.batch_alter_table("driver_cash_sessions", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_driver_cash_sessions_driver_active",
            postgresql_where=sa.text("status IN ('open', 'overdue')"),
            sqlite_where=sa.text("status IN ('open', 'overdue')"),
        )
        batch_op.create_index(
            "uq_driver_cash_sessions_driver_active",
            ["driver_user_id"],
            unique=True,
            postgresql_where=sa.text("status IN ('open', 'overdue', 'partial')"),
            sqlite_where=sa.text("status IN ('open', 'overdue', 'partial')"),
        )


def downgrade():
    with op.batch_alter_table("driver_cash_sessions", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_driver_cash_sessions_driver_active",
            postgresql_where=sa.text("status IN ('open', 'overdue', 'partial')"),
            sqlite_where=sa.text("status IN ('open', 'overdue', 'partial')"),
        )
        batch_op.create_index(
            "uq_driver_cash_sessions_driver_active",
            ["driver_user_id"],
            unique=True,
            postgresql_where=sa.text("status IN ('open', 'overdue')"),
            sqlite_where=sa.text("status IN ('open', 'overdue')"),
        )

    with op.batch_alter_table("driver_cash_handoffs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_driver_cash_handoffs_voided_by_user_id"))
        batch_op.drop_index(batch_op.f("ix_driver_cash_handoffs_recorded_by_user_id"))
        batch_op.drop_index(batch_op.f("ix_driver_cash_handoffs_driver_cash_session_id"))
        batch_op.drop_index("idx_driver_cash_handoffs_session_occurred")
    op.drop_table("driver_cash_handoffs")
