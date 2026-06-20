"""add order_items.is_reward_item

Revision ID: e1a2b3c4d5f6
Revises: d1f7a2c5e8b0
Create Date: 2026-06-20

Flags loyalty free-product reward lines so Click fiscalization and Asl Belgisi
marking-code accounting exclude them entirely.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "e1a2b3c4d5f6"
down_revision = "d1f7a2c5e8b0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "order_items",
        sa.Column(
            "is_reward_item",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade():
    op.drop_column("order_items", "is_reward_item")
