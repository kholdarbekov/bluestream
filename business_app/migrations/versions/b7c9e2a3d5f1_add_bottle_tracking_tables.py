"""Add bottle tracking tables

Revision ID: b7c9e2a3d5f1
Revises: 3f8812d340db
Create Date: 2026-04-12 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "b7c9e2a3d5f1"
down_revision = "3f8812d340db"
branch_labels = None
depends_on = None


bottle_ledger_event_type_enum = postgresql.ENUM(
    "delivery",
    "return_on_delivery",
    "standalone_collection",
    "admin_adjustment",
    "fine_issued",
    "fine_reversed",
    "initial_balance",
    name="bottle_ledger_event_type",
    create_type=False,
)

bottle_fine_status_enum = postgresql.ENUM(
    "pending",
    "invoiced",
    "paid",
    "waived",
    name="bottle_fine_status",
    create_type=False,
)


def upgrade():
    bind = op.get_bind()

    # Create enums
    bottle_ledger_event_type_enum.create(bind, checkfirst=True)
    bottle_fine_status_enum.create(bind, checkfirst=True)

    # --- bottle_balances ---
    op.create_table(
        "bottle_balances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("address_id", sa.Integer(), sa.ForeignKey("addresses.id"), nullable=False),
        sa.Column("balance", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("last_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_return_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "address_id", name="uq_bottle_balance_user_address"),
    )
    op.create_index("idx_bottle_balances_user", "bottle_balances", ["user_id"])
    op.create_index("idx_bottle_balances_address", "bottle_balances", ["address_id"])
    op.create_index("idx_bottle_balances_balance", "bottle_balances", ["balance"])

    # --- bottle_ledger ---
    op.create_table(
        "bottle_ledger",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("address_id", sa.Integer(), sa.ForeignKey("addresses.id"), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("delivery_id", sa.Integer(), sa.ForeignKey("deliveries.id"), nullable=True),
        sa.Column("event_type", bottle_ledger_event_type_enum, nullable=False),
        sa.Column("quantity", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("balance_after", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("entry_metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_bottle_ledger_idempotency"),
    )
    op.create_index("idx_bottle_ledger_user_created", "bottle_ledger", ["user_id", "created_at"])
    op.create_index("idx_bottle_ledger_address_created", "bottle_ledger", ["address_id", "created_at"])
    op.create_index("idx_bottle_ledger_order", "bottle_ledger", ["order_id"])
    op.create_index("idx_bottle_ledger_event_type", "bottle_ledger", ["event_type"])
    op.create_index(op.f("ix_bottle_ledger_user_id"), "bottle_ledger", ["user_id"])
    op.create_index(op.f("ix_bottle_ledger_address_id"), "bottle_ledger", ["address_id"])
    op.create_index(op.f("ix_bottle_ledger_delivery_id"), "bottle_ledger", ["delivery_id"])
    op.create_index(op.f("ix_bottle_ledger_actor_user_id"), "bottle_ledger", ["actor_user_id"])

    # --- bottle_fines ---
    op.create_table(
        "bottle_fines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("bottle_balance_id", sa.Integer(), sa.ForeignKey("bottle_balances.id"), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("fine_amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("status", bottle_fine_status_enum, nullable=False, server_default="pending"),
        sa.Column("issued_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("waived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("waived_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_bottle_fines_user_status", "bottle_fines", ["user_id", "status"])
    op.create_index("idx_bottle_fines_balance", "bottle_fines", ["bottle_balance_id"])

    # --- driver_bottle_loads ---
    op.create_table(
        "driver_bottle_loads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("driver_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("load_date", sa.Date(), nullable=False),
        sa.Column("bottles_loaded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bottles_delivered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bottles_collected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bottles_returned_to_warehouse", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discrepancy", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reconciled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("reconciled_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("driver_user_id", "load_date", name="uq_driver_bottle_load_date"),
    )
    op.create_index("idx_driver_bottle_loads_driver_date", "driver_bottle_loads", ["driver_user_id", "load_date"])


def downgrade():
    op.drop_table("driver_bottle_loads")
    op.drop_table("bottle_fines")
    op.drop_table("bottle_ledger")
    op.drop_table("bottle_balances")

    bind = op.get_bind()
    bottle_fine_status_enum.drop(bind, checkfirst=True)
    bottle_ledger_event_type_enum.drop(bind, checkfirst=True)
