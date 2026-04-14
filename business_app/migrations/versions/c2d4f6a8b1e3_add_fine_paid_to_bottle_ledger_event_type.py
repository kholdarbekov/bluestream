"""Add fine_paid to bottle_ledger_event_type enum

Revision ID: c2d4f6a8b1e3
Revises: b7c9e2a3d5f1
Create Date: 2026-04-13 00:00:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c2d4f6a8b1e3'
down_revision = 'b7c9e2a3d5f1'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TYPE bottle_ledger_event_type ADD VALUE IF NOT EXISTS 'fine_paid'"
    )


def downgrade():
    # PostgreSQL does not support removing individual enum values.
    # To fully reverse this, the type would need to be recreated — not safe in production.
    pass
