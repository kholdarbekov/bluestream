"""add cod receivable reconciliation ledger

Revision ID: 348515beb00c
Revises: 0328a3f13381
Create Date: 2026-03-06 16:40:43.265310

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "348515beb00c"
down_revision = "0328a3f13381"
branch_labels = None
depends_on = None


cash_collection_source_enum = sa.Enum(
    "delivery_completion",
    "next_delivery",
    "standalone_meeting",
    "admin_adjustment",
    "backfill",
    name="cash_collection_source",
)

driver_cash_session_status_enum = sa.Enum(
    "open",
    "submitted",
    "verified",
    "mismatch",
    "overdue",
    "resolved",
    name="driver_cash_session_status",
)


def upgrade():
    op.execute("ALTER TYPE payment_status ADD VALUE IF NOT EXISTS 'partially_paid'")

    op.create_table(
        "driver_cash_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=100), nullable=False),
        sa.Column("driver_user_id", sa.Integer(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("status", driver_cash_session_status_enum, nullable=False),
        sa.Column("session_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expected_cash", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("declared_cash", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("verified_cash", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("declared_variance", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("verified_variance", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_by_user_id", sa.Integer(), nullable=True),
        sa.Column("verified_by_user_id", sa.Integer(), nullable=True),
        sa.Column("blocked_from_cod", sa.Boolean(), nullable=False),
        sa.Column("block_reason", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("verification_notes", sa.Text(), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("resolution_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["driver_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["verified_by_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("driver_user_id", "business_date", name="uq_driver_cash_sessions_driver_date"),
    )
    with op.batch_alter_table("driver_cash_sessions", schema=None) as batch_op:
        batch_op.create_index("idx_driver_cash_sessions_driver_date", ["driver_user_id", "business_date"], unique=False)
        batch_op.create_index("idx_driver_cash_sessions_status_date", ["status", "business_date"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_driver_cash_sessions_blocked_from_cod"), ["blocked_from_cod"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_driver_cash_sessions_business_date"), ["business_date"], unique=False)
        batch_op.create_index(batch_op.f("ix_driver_cash_sessions_driver_user_id"), ["driver_user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_driver_cash_sessions_session_id"), ["session_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_driver_cash_sessions_status"), ["status"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_driver_cash_sessions_submitted_by_user_id"), ["submitted_by_user_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_driver_cash_sessions_verified_by_user_id"), ["verified_by_user_id"], unique=False
        )

    op.create_table(
        "cash_collection_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(length=100), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("collector_user_id", sa.Integer(), nullable=True),
        sa.Column("recorded_by_user_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("delivery_id", sa.Integer(), nullable=True),
        sa.Column("driver_cash_session_id", sa.Integer(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("source", cash_collection_source_enum, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("proof_data", sa.JSON(), nullable=True),
        sa.Column("unapplied_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_by_user_id", sa.Integer(), nullable=True),
        sa.Column("void_reason", sa.String(length=255), nullable=True),
        sa.Column("entry_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["collector_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["deliveries.id"],
        ),
        sa.ForeignKeyConstraint(
            ["driver_cash_session_id"],
            ["driver_cash_sessions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["voided_by_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_cash_collection_events_idempotency_key"),
    )
    with op.batch_alter_table("cash_collection_events", schema=None) as batch_op:
        batch_op.create_index(
            "idx_cash_collection_events_collector_occurred", ["collector_user_id", "occurred_at"], unique=False
        )
        batch_op.create_index(
            "idx_cash_collection_events_customer_created", ["customer_id", "created_at"], unique=False
        )
        batch_op.create_index("idx_cash_collection_events_source_occurred", ["source", "occurred_at"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_cash_collection_events_collector_user_id"), ["collector_user_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_cash_collection_events_customer_id"), ["customer_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_cash_collection_events_delivery_id"), ["delivery_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_cash_collection_events_driver_cash_session_id"), ["driver_cash_session_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_cash_collection_events_event_id"), ["event_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_cash_collection_events_occurred_at"), ["occurred_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_cash_collection_events_order_id"), ["order_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_cash_collection_events_recorded_by_user_id"), ["recorded_by_user_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_cash_collection_events_source"), ["source"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_cash_collection_events_voided_by_user_id"), ["voided_by_user_id"], unique=False
        )

    op.create_table(
        "cash_collection_allocations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cash_collection_event_id", sa.Integer(), nullable=False),
        sa.Column("payment_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("allocated_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("allocation_order", sa.Integer(), nullable=False),
        sa.Column("allocation_mode", sa.String(length=20), nullable=False),
        sa.Column("allocated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("reversal_reason", sa.String(length=255), nullable=True),
        sa.Column("allocation_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["cash_collection_event_id"],
            ["cash_collection_events.id"],
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["reversed_by_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cash_collection_event_id",
            "payment_id",
            "allocation_order",
            name="uq_cash_collection_allocations_event_payment_order",
        ),
    )
    with op.batch_alter_table("cash_collection_allocations", schema=None) as batch_op:
        batch_op.create_index(
            "idx_cash_collection_allocations_event_created", ["cash_collection_event_id", "created_at"], unique=False
        )
        batch_op.create_index(
            "idx_cash_collection_allocations_payment_created", ["payment_id", "created_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_cash_collection_allocations_cash_collection_event_id"),
            ["cash_collection_event_id"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_cash_collection_allocations_order_id"), ["order_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_cash_collection_allocations_payment_id"), ["payment_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_cash_collection_allocations_reversed_by_user_id"), ["reversed_by_user_id"], unique=False
        )

    with op.batch_alter_table("payments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("collected_by", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("amount_collected", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("outstanding_amount", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("last_collected_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index("idx_payments_method_status", ["payment_method", "status"], unique=False)
        batch_op.create_index("idx_payments_outstanding_status", ["outstanding_amount", "status"], unique=False)
        batch_op.create_index(batch_op.f("ix_payments_collected_by"), ["collected_by"], unique=False)
        batch_op.create_index(batch_op.f("ix_payments_outstanding_amount"), ["outstanding_amount"], unique=False)
        batch_op.create_foreign_key("fk_payments_collected_by_users", "users", ["collected_by"], ["id"])

    op.execute(
        """
        UPDATE payments
        SET
            amount_collected = CASE
                WHEN status IN ('completed', 'refunded', 'partially_refunded', 'cancelled')
                    THEN COALESCE(amount, 0)
                ELSE 0
            END,
            outstanding_amount = CASE
                WHEN status IN ('completed', 'refunded', 'partially_refunded', 'cancelled')
                    THEN 0
                ELSE COALESCE(amount, 0)
            END
        """
    )

    with op.batch_alter_table("payments", schema=None) as batch_op:
        batch_op.alter_column("amount_collected", server_default=None)
        batch_op.alter_column("outstanding_amount", server_default=None)


def downgrade():
    with op.batch_alter_table("payments", schema=None) as batch_op:
        batch_op.drop_constraint("fk_payments_collected_by_users", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_payments_outstanding_amount"))
        batch_op.drop_index(batch_op.f("ix_payments_collected_by"))
        batch_op.drop_index("idx_payments_outstanding_status")
        batch_op.drop_index("idx_payments_method_status")
        batch_op.drop_column("last_collected_at")
        batch_op.drop_column("outstanding_amount")
        batch_op.drop_column("amount_collected")
        batch_op.drop_column("collected_by")

    with op.batch_alter_table("cash_collection_allocations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_cash_collection_allocations_reversed_by_user_id"))
        batch_op.drop_index(batch_op.f("ix_cash_collection_allocations_payment_id"))
        batch_op.drop_index(batch_op.f("ix_cash_collection_allocations_order_id"))
        batch_op.drop_index(batch_op.f("ix_cash_collection_allocations_cash_collection_event_id"))
        batch_op.drop_index("idx_cash_collection_allocations_payment_created")
        batch_op.drop_index("idx_cash_collection_allocations_event_created")

    op.drop_table("cash_collection_allocations")
    with op.batch_alter_table("cash_collection_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_cash_collection_events_voided_by_user_id"))
        batch_op.drop_index(batch_op.f("ix_cash_collection_events_source"))
        batch_op.drop_index(batch_op.f("ix_cash_collection_events_recorded_by_user_id"))
        batch_op.drop_index(batch_op.f("ix_cash_collection_events_order_id"))
        batch_op.drop_index(batch_op.f("ix_cash_collection_events_occurred_at"))
        batch_op.drop_index(batch_op.f("ix_cash_collection_events_event_id"))
        batch_op.drop_index(batch_op.f("ix_cash_collection_events_driver_cash_session_id"))
        batch_op.drop_index(batch_op.f("ix_cash_collection_events_delivery_id"))
        batch_op.drop_index(batch_op.f("ix_cash_collection_events_customer_id"))
        batch_op.drop_index(batch_op.f("ix_cash_collection_events_collector_user_id"))
        batch_op.drop_index("idx_cash_collection_events_source_occurred")
        batch_op.drop_index("idx_cash_collection_events_customer_created")
        batch_op.drop_index("idx_cash_collection_events_collector_occurred")

    op.drop_table("cash_collection_events")
    with op.batch_alter_table("driver_cash_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_driver_cash_sessions_verified_by_user_id"))
        batch_op.drop_index(batch_op.f("ix_driver_cash_sessions_submitted_by_user_id"))
        batch_op.drop_index(batch_op.f("ix_driver_cash_sessions_status"))
        batch_op.drop_index(batch_op.f("ix_driver_cash_sessions_session_id"))
        batch_op.drop_index(batch_op.f("ix_driver_cash_sessions_driver_user_id"))
        batch_op.drop_index(batch_op.f("ix_driver_cash_sessions_business_date"))
        batch_op.drop_index(batch_op.f("ix_driver_cash_sessions_blocked_from_cod"))
        batch_op.drop_index("idx_driver_cash_sessions_status_date")
        batch_op.drop_index("idx_driver_cash_sessions_driver_date")

    op.drop_table("driver_cash_sessions")
    cash_collection_source_enum.drop(op.get_bind(), checkfirst=True)
    driver_cash_session_status_enum.drop(op.get_bind(), checkfirst=True)

    # PostgreSQL enum values cannot be removed safely in-place, so `payment_status.partially_paid`
    # is intentionally left behind on downgrade.
