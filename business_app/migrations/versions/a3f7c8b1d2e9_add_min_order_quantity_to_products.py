"""add min_order_quantity to products

Adds:
  - products.min_order_quantity INTEGER NOT NULL DEFAULT 1.

Per-product purchase minimum used by cart and order validation. Distinct from
PriceRule.min_quantity (bulk-discount tiers) and from min_stock_level (restock
threshold). Existing rows backfill to 1 via ``server_default="1"``; without it
the SQLAlchemy ``default=1`` only runs at ORM-insert time and existing rows
would violate the NOT NULL constraint. The server default is kept permanently
as a safety net.

On Postgres >= 11 ``ADD COLUMN ... NOT NULL DEFAULT 1`` is a metadata-only
operation -- no table rewrite -- so this is safe on the live products table.

Revision ID: a3f7c8b1d2e9
Revises: e2c5a8b1f9d4
Create Date: 2026-05-01 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a3f7c8b1d2e9"
down_revision = "e2c5a8b1f9d4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "min_order_quantity",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )


def downgrade():
    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.drop_column("min_order_quantity")
