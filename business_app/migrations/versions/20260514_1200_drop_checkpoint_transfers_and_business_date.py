"""drop checkpoint transfers and legacy business_date

Folds historical checkpoint-transfer totals into ``expected_cash_on_hand`` on
the session, then drops the ``driver_cash_transfers`` table and the legacy
``business_date`` / ``transferred_cash_total`` columns on
``driver_cash_sessions``. Also clears stale risk-flag entries and the
``transfer_variance_settled`` resolution reason code, which no longer have a
write path.

Revision ID: 7e2a4f1c9b5d
Revises: c8f8f9b6adf5
Create Date: 2026-05-14 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "7e2a4f1c9b5d"
down_revision = "c8f8f9b6adf5"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    # 1. Backfill expected_cash_on_hand so historical sessions stay internally
    #    consistent after the transfers table is gone. The legacy
    #    refresh_expected_cash() formula was gross - transferred_total, so
    #    that is the authoritative value to freeze on the row.
    bind.execute(
        sa.text(
            """
            UPDATE driver_cash_sessions ds
            SET expected_cash_on_hand = COALESCE(ds.gross_cash_collected, 0)
                                       - COALESCE(t.transferred_total, 0)
            FROM (
                SELECT driver_cash_session_id,
                       SUM(COALESCE(counted_transfer_cash, declared_transfer_cash)) AS transferred_total
                FROM driver_cash_transfers
                WHERE transfer_status IN ('confirmed', 'disputed')
                  AND checkpoint_confirmed_at IS NOT NULL
                GROUP BY driver_cash_session_id
            ) t
            WHERE ds.id = t.driver_cash_session_id
            """
        )
    )

    # 2. Drop the transfers table (mirror downgrade of 7775db6340cf).
    with op.batch_alter_table("driver_cash_transfers", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_driver_cash_transfers_transfer_status"))
        batch_op.drop_index(batch_op.f("ix_driver_cash_transfers_transfer_id"))
        batch_op.drop_index(batch_op.f("ix_driver_cash_transfers_driver_user_id"))
        batch_op.drop_index(batch_op.f("ix_driver_cash_transfers_driver_confirmed_by_user_id"))
        batch_op.drop_index(batch_op.f("ix_driver_cash_transfers_driver_cash_session_id"))
        batch_op.drop_index(batch_op.f("ix_driver_cash_transfers_checkpoint_confirmed_by_user_id"))
        batch_op.drop_index("idx_driver_cash_transfers_status_created")
        batch_op.drop_index("idx_driver_cash_transfers_session_created")
    op.drop_table("driver_cash_transfers")

    # 3. Drop legacy daily indexes & column on driver_cash_sessions.
    with op.batch_alter_table("driver_cash_sessions", schema=None) as batch_op:
        batch_op.drop_index("idx_driver_cash_sessions_driver_date")
        batch_op.drop_index("idx_driver_cash_sessions_status_date")
        batch_op.drop_index(batch_op.f("ix_driver_cash_sessions_business_date"))
        batch_op.drop_column("business_date")
        batch_op.drop_column("transferred_cash_total")

    # 4. Retire dead resolution-reason and risk-flag values that no longer
    #    have a write path. Convert them to neutral fallbacks so existing
    #    rows remain valid against the trimmed RESOLUTION_REASON_CODES set
    #    and risk_flags don't reference checkpoint-transfer concepts.
    bind.execute(
        sa.text(
            """
            UPDATE driver_cash_sessions
            SET resolution_reason_code = 'clerical_correction'
            WHERE resolution_reason_code = 'transfer_variance_settled'
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE driver_cash_sessions ds
            SET risk_flags = COALESCE(
                (
                    SELECT json_agg(value)
                    FROM json_array_elements(ds.risk_flags) AS value
                    WHERE value::text NOT IN (
                        '"pending_transfer_confirmation"',
                        '"transfer_variance_detected"'
                    )
                ),
                '[]'::json
            )
            WHERE ds.risk_flags::text LIKE '%transfer%'
            """
        )
    )


def downgrade():
    """
    Recreate the schema. Historical transfer rows cannot be restored — only
    the structure is brought back.
    """
    with op.batch_alter_table("driver_cash_sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("business_date", sa.Date(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "transferred_cash_total",
                sa.Numeric(precision=12, scale=2),
                nullable=False,
                server_default="0",
            )
        )

    op.execute(
        "UPDATE driver_cash_sessions "
        "SET business_date = (session_started_at AT TIME ZONE 'UTC')::date "
        "WHERE business_date IS NULL"
    )

    with op.batch_alter_table("driver_cash_sessions", schema=None) as batch_op:
        batch_op.alter_column("business_date", nullable=False)
        batch_op.alter_column("transferred_cash_total", server_default=None)
        batch_op.create_index(
            "idx_driver_cash_sessions_driver_date",
            ["driver_user_id", "business_date"],
            unique=False,
        )
        batch_op.create_index(
            "idx_driver_cash_sessions_status_date",
            ["status", "business_date"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_driver_cash_sessions_business_date"),
            ["business_date"],
            unique=False,
        )

    op.create_table(
        "driver_cash_transfers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transfer_id", sa.String(length=100), nullable=False),
        sa.Column("driver_cash_session_id", sa.Integer(), nullable=False),
        sa.Column("driver_user_id", sa.Integer(), nullable=False),
        sa.Column("declared_transfer_cash", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("counted_transfer_cash", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("transfer_variance", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("transfer_status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("transfer_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("driver_confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("driver_confirmed_by_user_id", sa.Integer(), nullable=False),
        sa.Column("checkpoint_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checkpoint_confirmed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["checkpoint_confirmed_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["driver_cash_session_id"], ["driver_cash_sessions.id"]),
        sa.ForeignKeyConstraint(["driver_confirmed_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["driver_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("driver_cash_transfers", schema=None) as batch_op:
        batch_op.create_index(
            "idx_driver_cash_transfers_session_created",
            ["driver_cash_session_id", "created_at"],
            unique=False,
        )
        batch_op.create_index(
            "idx_driver_cash_transfers_status_created",
            ["transfer_status", "created_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_driver_cash_transfers_checkpoint_confirmed_by_user_id"),
            ["checkpoint_confirmed_by_user_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_driver_cash_transfers_driver_cash_session_id"),
            ["driver_cash_session_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_driver_cash_transfers_driver_confirmed_by_user_id"),
            ["driver_confirmed_by_user_id"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_driver_cash_transfers_driver_user_id"), ["driver_user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_driver_cash_transfers_transfer_id"), ["transfer_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_driver_cash_transfers_transfer_status"), ["transfer_status"], unique=False)
        batch_op.alter_column("declared_transfer_cash", server_default=None)
        batch_op.alter_column("transfer_variance", server_default=None)
        batch_op.alter_column("transfer_status", server_default=None)
        batch_op.alter_column("transfer_metadata", server_default=None)
