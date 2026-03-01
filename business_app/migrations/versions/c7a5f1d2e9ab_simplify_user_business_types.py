"""simplify user business types to individual/business

Revision ID: c7a5f1d2e9ab
Revises: b2c9f1e4a7b3
Create Date: 2026-02-28 07:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c7a5f1d2e9ab"
down_revision = "b2c9f1e4a7b3"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        UPDATE users
        SET business_type = CASE
            WHEN lower(trim(coalesce(business_type, ''))) IN ('business', 'corporation', 'small_business', 'non_profit', 'government')
                THEN 'business'
            ELSE 'individual'
        END
        """
    )
    op.alter_column(
        "users",
        "business_type",
        existing_type=sa.String(length=50),
        nullable=False,
        server_default="individual",
    )


def downgrade():
    op.alter_column(
        "users",
        "business_type",
        existing_type=sa.String(length=50),
        nullable=True,
        server_default=None,
    )
    op.execute(
        """
        UPDATE users
        SET business_type = NULL
        WHERE business_type = 'individual'
        """
    )
