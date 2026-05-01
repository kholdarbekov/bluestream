"""add corporate contracts and prepayment ledger

Revision ID: b2c9f1e4a7b3
Revises: cea8f329e11e
Create Date: 2026-02-27 17:40:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "b2c9f1e4a7b3"
down_revision = "cea8f329e11e"
branch_labels = None
depends_on = None


def upgrade():
    contract_status_enum = postgresql.ENUM(
        "draft",
        "active",
        "suspended",
        "terminated",
        name="corporate_contract_status",
        create_type=False,
    )
    prepayment_event_enum = postgresql.ENUM(
        "topup",
        "reserve",
        "consume",
        "release",
        "adjustment",
        name="corporate_prepayment_event_type",
        create_type=False,
    )

    contract_status_enum.create(op.get_bind(), checkfirst=True)
    prepayment_event_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "corporate_contracts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("contract_number", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", contract_status_enum, nullable=False),
        sa.Column("start_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("bank_details", sa.JSON(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contract_number"),
    )
    with op.batch_alter_table("corporate_contracts", schema=None) as batch_op:
        batch_op.create_index("idx_corporate_contracts_active", ["is_active"], unique=False)
        batch_op.create_index("idx_corporate_contracts_user_status", ["user_id", "status"], unique=False)
        batch_op.create_index(batch_op.f("ix_corporate_contracts_contract_number"), ["contract_number"], unique=True)
        batch_op.create_index(batch_op.f("ix_corporate_contracts_is_active"), ["is_active"], unique=False)
        batch_op.create_index(batch_op.f("ix_corporate_contracts_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_corporate_contracts_user_id"), ["user_id"], unique=False)

    op.create_table(
        "corporate_contract_product_prices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("contract_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("is_prepayment_eligible", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["contract_id"], ["corporate_contracts.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contract_id", "product_id", name="uq_corporate_contract_product_price"),
    )
    with op.batch_alter_table("corporate_contract_product_prices", schema=None) as batch_op:
        batch_op.create_index(
            "idx_corporate_contract_product_prices_contract",
            ["contract_id", "is_active"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_corporate_contract_product_prices_contract_id"), ["contract_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_corporate_contract_product_prices_is_active"), ["is_active"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_corporate_contract_product_prices_is_prepayment_eligible"),
            ["is_prepayment_eligible"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_corporate_contract_product_prices_product_id"), ["product_id"], unique=False
        )

    op.create_table(
        "corporate_prepayment_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("contract_id", sa.Integer(), nullable=False),
        sa.Column("prepaid_units", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("reserved_units", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("consumed_units", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_topup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["contract_id"], ["corporate_contracts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contract_id", name="uq_corporate_prepayment_account_contract"),
    )
    with op.batch_alter_table("corporate_prepayment_accounts", schema=None) as batch_op:
        batch_op.create_index(
            "idx_corporate_prepayment_accounts_contract_active",
            ["contract_id", "is_active"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_corporate_prepayment_accounts_contract_id"), ["contract_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_corporate_prepayment_accounts_is_active"), ["is_active"], unique=False)

    op.create_table(
        "corporate_prepayment_ledger",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("contract_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("delivery_id", sa.Integer(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("event_type", prepayment_event_enum, nullable=False),
        sa.Column("units", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("transfer_reference", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("entry_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["corporate_prepayment_accounts.id"]),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["contract_id"], ["corporate_contracts.id"]),
        sa.ForeignKeyConstraint(["delivery_id"], ["deliveries.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_corporate_prepayment_ledger_idempotency"),
    )
    with op.batch_alter_table("corporate_prepayment_ledger", schema=None) as batch_op:
        batch_op.create_index(
            "idx_corporate_prepayment_ledger_contract_created",
            ["contract_id", "created_at"],
            unique=False,
        )
        batch_op.create_index(
            "idx_corporate_prepayment_ledger_order_event",
            ["order_id", "event_type"],
            unique=False,
        )
        batch_op.create_index(
            "idx_corporate_prepayment_ledger_delivery_event",
            ["delivery_id", "event_type"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_corporate_prepayment_ledger_account_id"), ["account_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_corporate_prepayment_ledger_actor_user_id"), ["actor_user_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_corporate_prepayment_ledger_contract_id"), ["contract_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_corporate_prepayment_ledger_delivery_id"), ["delivery_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_corporate_prepayment_ledger_event_type"), ["event_type"], unique=False)
        batch_op.create_index(batch_op.f("ix_corporate_prepayment_ledger_order_id"), ["order_id"], unique=False)


def downgrade():
    with op.batch_alter_table("corporate_prepayment_ledger", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_corporate_prepayment_ledger_order_id"))
        batch_op.drop_index(batch_op.f("ix_corporate_prepayment_ledger_event_type"))
        batch_op.drop_index(batch_op.f("ix_corporate_prepayment_ledger_delivery_id"))
        batch_op.drop_index(batch_op.f("ix_corporate_prepayment_ledger_contract_id"))
        batch_op.drop_index(batch_op.f("ix_corporate_prepayment_ledger_actor_user_id"))
        batch_op.drop_index(batch_op.f("ix_corporate_prepayment_ledger_account_id"))
        batch_op.drop_index("idx_corporate_prepayment_ledger_delivery_event")
        batch_op.drop_index("idx_corporate_prepayment_ledger_order_event")
        batch_op.drop_index("idx_corporate_prepayment_ledger_contract_created")

    op.drop_table("corporate_prepayment_ledger")

    with op.batch_alter_table("corporate_prepayment_accounts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_corporate_prepayment_accounts_is_active"))
        batch_op.drop_index(batch_op.f("ix_corporate_prepayment_accounts_contract_id"))
        batch_op.drop_index("idx_corporate_prepayment_accounts_contract_active")

    op.drop_table("corporate_prepayment_accounts")

    with op.batch_alter_table("corporate_contract_product_prices", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_corporate_contract_product_prices_product_id"))
        batch_op.drop_index(batch_op.f("ix_corporate_contract_product_prices_is_prepayment_eligible"))
        batch_op.drop_index(batch_op.f("ix_corporate_contract_product_prices_is_active"))
        batch_op.drop_index(batch_op.f("ix_corporate_contract_product_prices_contract_id"))
        batch_op.drop_index("idx_corporate_contract_product_prices_contract")

    op.drop_table("corporate_contract_product_prices")

    with op.batch_alter_table("corporate_contracts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_corporate_contracts_user_id"))
        batch_op.drop_index(batch_op.f("ix_corporate_contracts_status"))
        batch_op.drop_index(batch_op.f("ix_corporate_contracts_is_active"))
        batch_op.drop_index(batch_op.f("ix_corporate_contracts_contract_number"))
        batch_op.drop_index("idx_corporate_contracts_user_status")
        batch_op.drop_index("idx_corporate_contracts_active")

    op.drop_table("corporate_contracts")

    op.execute("DROP TYPE IF EXISTS corporate_prepayment_event_type")
    op.execute("DROP TYPE IF EXISTS corporate_contract_status")
