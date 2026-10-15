"""split corporate contract product price into business-account and cash prices

Revision ID: e7b2c4d9a1f6
Revises: c5d8e2f1a7b3
Create Date: 2026-10-09 15:00:00.000000

Spec: docs/superpowers/specs/2026-10-09-corporate-contract-dual-pricing-design.md §3.

corporate_contract_product_prices.unit_price becomes business_account_unit_price (nullable), and
cash_unit_price (nullable) is added and backfilled from it, so every existing row carries its
current price in both (owner ruling D4). NULL means "the catalogue price for this payment
method", looked up live at order time. Both columns get a named non-negative CHECK.

Rollback strategy:
    downgrade() restores unit_price = COALESCE(business_account_unit_price, cash_unit_price,
    products.base_price), puts NOT NULL back and drops cash_unit_price and both CHECKs. LOSSY: a
    cash price that differs from the bank-transfer price is discarded, and a row whose two prices
    were both empty comes back pinned at the product's base price as of the downgrade.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e7b2c4d9a1f6"
down_revision = "c5d8e2f1a7b3"
branch_labels = None
depends_on = None

TABLE = "corporate_contract_product_prices"
PRICE_TYPE = sa.Numeric(precision=10, scale=2)
CK_BUSINESS_ACCOUNT = "ck_corporate_contract_product_prices_ba_price_nonneg"
CK_CASH = "ck_corporate_contract_product_prices_cash_price_nonneg"


def upgrade():
    op.add_column(TABLE, sa.Column("cash_unit_price", PRICE_TYPE, nullable=True))
    op.execute(sa.text(f"UPDATE {TABLE} SET cash_unit_price = unit_price"))
    op.alter_column(TABLE, "unit_price", existing_type=PRICE_TYPE, nullable=True)
    op.alter_column(TABLE, "unit_price", existing_type=PRICE_TYPE, new_column_name="business_account_unit_price")
    op.create_check_constraint(
        CK_BUSINESS_ACCOUNT, TABLE, "business_account_unit_price IS NULL OR business_account_unit_price >= 0"
    )
    op.create_check_constraint(CK_CASH, TABLE, "cash_unit_price IS NULL OR cash_unit_price >= 0")


def downgrade():
    op.drop_constraint(CK_CASH, TABLE, type_="check")
    op.drop_constraint(CK_BUSINESS_ACCOUNT, TABLE, type_="check")
    op.execute(
        sa.text(
            f"UPDATE {TABLE} AS p "
            "SET business_account_unit_price = COALESCE(p.business_account_unit_price, p.cash_unit_price, pr.base_price) "
            "FROM products AS pr WHERE pr.id = p.product_id"
        )
    )
    op.alter_column(TABLE, "business_account_unit_price", existing_type=PRICE_TYPE, new_column_name="unit_price")
    op.alter_column(TABLE, "unit_price", existing_type=PRICE_TYPE, nullable=False)
    op.drop_column(TABLE, "cash_unit_price")
