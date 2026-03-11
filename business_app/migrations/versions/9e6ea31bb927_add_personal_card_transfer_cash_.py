"""add personal_card_transfer cash collection source

Revision ID: 9e6ea31bb927
Revises: 7775db6340cf
Create Date: 2026-03-11 15:43:14.888141

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = '9e6ea31bb927'
down_revision = '7775db6340cf'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TYPE cash_collection_source ADD VALUE IF NOT EXISTS 'personal_card_transfer'")


def downgrade():
    # PostgreSQL enum values cannot be removed safely in-place.
    pass
