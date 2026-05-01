"""Add driver bottle sessions, session orders, and transfers tables

Revision ID: d1e3f5a7b9c2
Revises: c2d4f6a8b1e3
Create Date: 2026-04-13 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "d1e3f5a7b9c2"
down_revision = "c2d4f6a8b1e3"
branch_labels = None
depends_on = None


driver_bottle_session_status_enum = postgresql.ENUM(
    "open",
    "closed",
    "force_closed",
    "cancelled",
    name="driver_bottle_session_status",
    create_type=False,
)

driver_bottle_transfer_status_enum = postgresql.ENUM(
    "pending",
    "confirmed",
    "disputed",
    "resolved",
    name="driver_bottle_transfer_status",
    create_type=False,
)


def upgrade():
    # Create new enums (idempotent — safe to re-run if type already exists)
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE driver_bottle_session_status AS ENUM ('open', 'closed', 'force_closed', 'cancelled');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """
    )
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE driver_bottle_transfer_status AS ENUM ('pending', 'confirmed', 'disputed', 'resolved');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """
    )

    # --- driver_bottle_sessions ---
    op.create_table(
        "driver_bottle_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_ref", sa.String(100), nullable=False, unique=True),
        sa.Column("driver_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "open", "closed", "force_closed", "cancelled", name="driver_bottle_session_status", create_type=False
            ),
            nullable=False,
            server_default="open",
        ),
        # Load side
        sa.Column("bottles_loaded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("loaded_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # Auto-tallied
        sa.Column("bottles_delivered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bottles_collected_from_customers", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bottles_transferred_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bottles_transferred_in", sa.Integer(), nullable=False, server_default="0"),
        # Close side (nullable until closed)
        sa.Column("bottles_returned_to_warehouse", sa.Integer(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("discrepancy", sa.Integer(), nullable=True),
        # Admin override
        sa.Column("force_closed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("force_close_reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("session_metadata", postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_dbs_driver_status", "driver_bottle_sessions", ["driver_user_id", "status"])
    op.create_index("idx_dbs_driver_started", "driver_bottle_sessions", ["driver_user_id", "started_at"])
    op.create_index("idx_dbs_status_started", "driver_bottle_sessions", ["status", "started_at"])
    # Partial unique index: at most one OPEN session per driver
    op.create_index(
        "uq_dbs_driver_open",
        "driver_bottle_sessions",
        ["driver_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )

    # --- driver_bottle_session_orders ---
    op.create_table(
        "driver_bottle_session_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("driver_bottle_sessions.id"), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", name="uq_dbso_order"),
    )
    op.create_index("idx_dbso_session", "driver_bottle_session_orders", ["session_id"])
    op.create_index("idx_dbso_order", "driver_bottle_session_orders", ["order_id"])

    # --- driver_bottle_transfers ---
    op.create_table(
        "driver_bottle_transfers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transfer_ref", sa.String(100), nullable=False, unique=True),
        sa.Column("sender_session_id", sa.Integer(), sa.ForeignKey("driver_bottle_sessions.id"), nullable=False),
        sa.Column("sender_driver_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("receiver_driver_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("receiver_session_id", sa.Integer(), sa.ForeignKey("driver_bottle_sessions.id"), nullable=True),
        sa.Column("declared_quantity", sa.Integer(), nullable=False),
        sa.Column("confirmed_quantity", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending", "confirmed", "disputed", "resolved", name="driver_bottle_transfer_status", create_type=False
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("dispute_notes", sa.Text(), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_dbt_sender_session", "driver_bottle_transfers", ["sender_session_id"])
    op.create_index("idx_dbt_receiver_session", "driver_bottle_transfers", ["receiver_session_id"])
    op.create_index("idx_dbt_receiver_driver", "driver_bottle_transfers", ["receiver_driver_id"])
    op.create_index("idx_dbt_status_created", "driver_bottle_transfers", ["status", "created_at"])


def downgrade():
    op.drop_table("driver_bottle_transfers")
    op.drop_table("driver_bottle_session_orders")
    op.drop_table("driver_bottle_sessions")

    bind = op.get_bind()
    driver_bottle_transfer_status_enum.drop(bind, checkfirst=True)
    driver_bottle_session_status_enum.drop(bind, checkfirst=True)
