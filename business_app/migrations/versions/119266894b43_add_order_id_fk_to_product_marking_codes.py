"""Add order_id FK to product_marking_codes

Revision ID: 119266894b43
Revises: d1e3f5a7b9c2
Create Date: 2026-04-14 16:18:45.961712

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '119266894b43'
down_revision = 'd1e3f5a7b9c2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('product_marking_codes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('order_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_product_marking_codes_order_id'), ['order_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_product_marking_codes_order_id',
            'orders',
            ['order_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade():
    with op.batch_alter_table('product_marking_codes', schema=None) as batch_op:
        batch_op.drop_constraint('fk_product_marking_codes_order_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_product_marking_codes_order_id'))
        batch_op.drop_column('order_id')
