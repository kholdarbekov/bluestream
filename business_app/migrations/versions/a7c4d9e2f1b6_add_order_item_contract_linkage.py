"""add order item contract linkage

Revision ID: a7c4d9e2f1b6
Revises: f4b8c2d9a1e7
Create Date: 2026-02-28 18:53:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a7c4d9e2f1b6"
down_revision = "f4b8c2d9a1e7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("order_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("contract_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("contract_product_price_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_order_items_contract_id", "corporate_contracts", ["contract_id"], ["id"])
        batch_op.create_foreign_key(
            "fk_order_items_contract_product_price_id",
            "corporate_contract_product_prices",
            ["contract_product_price_id"],
            ["id"],
        )
        batch_op.create_index(batch_op.f("ix_order_items_contract_id"), ["contract_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_order_items_contract_product_price_id"), ["contract_product_price_id"], unique=False)


def downgrade():
    with op.batch_alter_table("order_items", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_order_items_contract_product_price_id"))
        batch_op.drop_index(batch_op.f("ix_order_items_contract_id"))
        batch_op.drop_constraint("fk_order_items_contract_product_price_id", type_="foreignkey")
        batch_op.drop_constraint("fk_order_items_contract_id", type_="foreignkey")
        batch_op.drop_column("contract_product_price_id")
        batch_op.drop_column("contract_id")
