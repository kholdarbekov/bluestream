"""drop dead voucher_code column from loyalty_rewards

Revision ID: d9e3b5a7c1f2
Revises: c8a1f3e5b7d9
Create Date: 2026-06-16

The 'voucher' and 'free_delivery' reward types were never applied or redeemable
(apply_reward_to_order only handles discount/free_product) and have been removed
from the admin UI + API. voucher_code was the only voucher-specific column and is
unused (zero rows). Drop it. Downgrade re-adds the nullable column.
"""

from alembic import op
import sqlalchemy as sa

revision = "d9e3b5a7c1f2"
down_revision = "c8a1f3e5b7d9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("loyalty_rewards", schema=None) as batch_op:
        batch_op.drop_column("voucher_code")


def downgrade():
    with op.batch_alter_table("loyalty_rewards", schema=None) as batch_op:
        batch_op.add_column(sa.Column("voucher_code", sa.String(length=50), nullable=True))
