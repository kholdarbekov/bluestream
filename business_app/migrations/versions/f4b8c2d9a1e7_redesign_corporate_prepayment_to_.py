"""redesign corporate prepayment to product scoped balances

Revision ID: f4b8c2d9a1e7
Revises: e1f9a6c4b2d1
Create Date: 2026-02-28 18:12:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f4b8c2d9a1e7"
down_revision = "e1f9a6c4b2d1"
branch_labels = None
depends_on = None


def _legacy_accounts(bind):
    return bind.execute(
        sa.text(
            """
            SELECT id, contract_id, prepaid_units, reserved_units, consumed_units, last_topup_at
            FROM corporate_prepayment_accounts
            """
        )
    ).mappings().all()

def _legacy_ledger_count(bind, account_id):
    return bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM corporate_prepayment_ledger
            WHERE account_id = :account_id
            """
        ),
        {"account_id": account_id},
    ).scalar_one()


def upgrade():
    op.create_table(
        "corporate_prepayment_legacy_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("contract_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("prepaid_units", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("reserved_units", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("consumed_units", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("legacy_ledger_entries_count", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("migrated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["corporate_prepayment_accounts.id"]),
        sa.ForeignKeyConstraint(["contract_id"], ["corporate_contracts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", name="uq_corporate_prepayment_legacy_snapshot_account"),
    )
    with op.batch_alter_table("corporate_prepayment_legacy_snapshots", schema=None) as batch_op:
        batch_op.create_index("idx_corporate_prepayment_legacy_snapshots_contract", ["contract_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_corporate_prepayment_legacy_snapshots_account_id"), ["account_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_corporate_prepayment_legacy_snapshots_contract_id"), ["contract_id"], unique=False)

    op.create_table(
        "corporate_prepayment_balances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("prepaid_units", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("reserved_units", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("consumed_units", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_topup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["corporate_prepayment_accounts.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "product_id", name="uq_corporate_prepayment_balance_account_product"),
    )
    with op.batch_alter_table("corporate_prepayment_balances", schema=None) as batch_op:
        batch_op.create_index("idx_corporate_prepayment_balances_account_active", ["account_id", "is_active"], unique=False)
        batch_op.create_index("idx_corporate_prepayment_balances_product", ["product_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_corporate_prepayment_balances_account_id"), ["account_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_corporate_prepayment_balances_is_active"), ["is_active"], unique=False)
        batch_op.create_index(batch_op.f("ix_corporate_prepayment_balances_product_id"), ["product_id"], unique=False)

    with op.batch_alter_table("corporate_prepayment_ledger", schema=None) as batch_op:
        batch_op.add_column(sa.Column("balance_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("product_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("order_item_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("unit_price_snapshot", sa.Numeric(precision=12, scale=2), nullable=True))
        batch_op.create_foreign_key("fk_corporate_prepayment_ledger_balance_id", "corporate_prepayment_balances", ["balance_id"], ["id"])
        batch_op.create_foreign_key("fk_corporate_prepayment_ledger_product_id", "products", ["product_id"], ["id"])
        batch_op.create_foreign_key("fk_corporate_prepayment_ledger_order_item_id", "order_items", ["order_item_id"], ["id"])
        batch_op.create_index("idx_corporate_prepayment_ledger_product_event", ["product_id", "event_type"], unique=False)
        batch_op.create_index(batch_op.f("ix_corporate_prepayment_ledger_balance_id"), ["balance_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_corporate_prepayment_ledger_product_id"), ["product_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_corporate_prepayment_ledger_order_item_id"), ["order_item_id"], unique=False)

    bind = op.get_bind()
    for account in _legacy_accounts(bind):
        prepaid_units = account["prepaid_units"] or 0
        reserved_units = account["reserved_units"] or 0
        consumed_units = account["consumed_units"] or 0
        legacy_ledger_count = _legacy_ledger_count(bind, account["id"])
        has_legacy_balance = any(value and value != 0 for value in [prepaid_units, reserved_units, consumed_units])
        has_legacy_ledger = legacy_ledger_count > 0
        if not has_legacy_balance and not has_legacy_ledger:
            continue

        bind.execute(
            sa.text(
                """
                INSERT INTO corporate_prepayment_legacy_snapshots (
                    contract_id,
                    account_id,
                    prepaid_units,
                    reserved_units,
                    consumed_units,
                    legacy_ledger_entries_count,
                    notes,
                    migrated_at
                )
                VALUES (
                    :contract_id,
                    :account_id,
                    :prepaid_units,
                    :reserved_units,
                    :consumed_units,
                    :legacy_ledger_entries_count,
                    :notes,
                    NOW()
                )
                """
            ),
            {
                "contract_id": account["contract_id"],
                "account_id": account["id"],
                "prepaid_units": prepaid_units,
                "reserved_units": reserved_units,
                "consumed_units": consumed_units,
                "legacy_ledger_entries_count": legacy_ledger_count,
                "notes": (
                    "Legacy aggregate corporate prepayment state archived during product-scoped redesign. "
                    "No historical product-level backfill was performed."
                ),
            },
        )

    with op.batch_alter_table("corporate_prepayment_accounts", schema=None) as batch_op:
        batch_op.drop_column("prepaid_units")
        batch_op.drop_column("reserved_units")
        batch_op.drop_column("consumed_units")


def downgrade():
    with op.batch_alter_table("corporate_prepayment_accounts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("consumed_units", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"))
        batch_op.add_column(sa.Column("reserved_units", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"))
        batch_op.add_column(sa.Column("prepaid_units", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"))

    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE corporate_prepayment_accounts AS account
            SET prepaid_units = COALESCE(summary.prepaid_units, legacy.prepaid_units, 0),
                reserved_units = COALESCE(summary.reserved_units, legacy.reserved_units, 0),
                consumed_units = COALESCE(summary.consumed_units, legacy.consumed_units, 0)
            FROM (
                SELECT
                    account_id,
                    SUM(prepaid_units) AS prepaid_units,
                    SUM(reserved_units) AS reserved_units,
                    SUM(consumed_units) AS consumed_units
                FROM corporate_prepayment_balances
                GROUP BY account_id
            ) AS summary
            FULL OUTER JOIN corporate_prepayment_legacy_snapshots AS legacy
                ON legacy.account_id = summary.account_id
            WHERE COALESCE(summary.account_id, legacy.account_id) = account.id
            """
        )
    )

    with op.batch_alter_table("corporate_prepayment_ledger", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_corporate_prepayment_ledger_order_item_id"))
        batch_op.drop_index(batch_op.f("ix_corporate_prepayment_ledger_product_id"))
        batch_op.drop_index(batch_op.f("ix_corporate_prepayment_ledger_balance_id"))
        batch_op.drop_index("idx_corporate_prepayment_ledger_product_event")
        batch_op.drop_constraint("fk_corporate_prepayment_ledger_order_item_id", type_="foreignkey")
        batch_op.drop_constraint("fk_corporate_prepayment_ledger_product_id", type_="foreignkey")
        batch_op.drop_constraint("fk_corporate_prepayment_ledger_balance_id", type_="foreignkey")
        batch_op.drop_column("unit_price_snapshot")
        batch_op.drop_column("order_item_id")
        batch_op.drop_column("product_id")
        batch_op.drop_column("balance_id")

    with op.batch_alter_table("corporate_prepayment_balances", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_corporate_prepayment_balances_product_id"))
        batch_op.drop_index(batch_op.f("ix_corporate_prepayment_balances_is_active"))
        batch_op.drop_index(batch_op.f("ix_corporate_prepayment_balances_account_id"))
        batch_op.drop_index("idx_corporate_prepayment_balances_product")
        batch_op.drop_index("idx_corporate_prepayment_balances_account_active")

    op.drop_table("corporate_prepayment_balances")

    with op.batch_alter_table("corporate_prepayment_legacy_snapshots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_corporate_prepayment_legacy_snapshots_contract_id"))
        batch_op.drop_index(batch_op.f("ix_corporate_prepayment_legacy_snapshots_account_id"))
        batch_op.drop_index("idx_corporate_prepayment_legacy_snapshots_contract")

    op.drop_table("corporate_prepayment_legacy_snapshots")
