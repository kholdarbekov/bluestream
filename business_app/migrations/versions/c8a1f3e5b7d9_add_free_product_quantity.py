"""add free_product_quantity to loyalty_rewards

Revision ID: c8a1f3e5b7d9
Revises: b7d4e9f1c2a3
Create Date: 2026-06-15

server_default="1" ensures existing loyalty_rewards rows get a sane quantity (1 free
unit) without requiring a NOT NULL backfill.  The column stays nullable so non-
free_product reward types (discount, voucher, free_delivery) can leave it unset.
"""

from alembic import op
import sqlalchemy as sa

revision = "c8a1f3e5b7d9"
down_revision = "b7d4e9f1c2a3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("loyalty_rewards", schema=None) as batch_op:
        batch_op.add_column(sa.Column("free_product_quantity", sa.Integer(), nullable=True, server_default="1"))


def downgrade():
    with op.batch_alter_table("loyalty_rewards", schema=None) as batch_op:
        batch_op.drop_column("free_product_quantity")
