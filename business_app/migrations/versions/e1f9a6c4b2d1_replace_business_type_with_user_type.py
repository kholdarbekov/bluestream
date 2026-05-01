"""replace business_type with user_type enum

Revision ID: e1f9a6c4b2d1
Revises: c7a5f1d2e9ab
Create Date: 2026-02-28 10:35:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "e1f9a6c4b2d1"
down_revision = "c7a5f1d2e9ab"
branch_labels = None
depends_on = None


def upgrade():
    user_type_enum = postgresql.ENUM(
        "individual",
        "entity",
        "staff",
        name="user_type",
        create_type=False,
    )
    user_type_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "users",
        sa.Column("user_type", user_type_enum, nullable=True, server_default="individual"),
    )

    op.execute(
        """
        UPDATE users
        SET user_type = CASE
            WHEN lower(trim(coalesce(role::text, ''))) IN ('admin', 'manager', 'operator', 'delivery_driver')
                OR (
                    json_typeof(COALESCE(staff_roles, '[]'::json)) = 'array'
                    AND json_array_length(COALESCE(staff_roles, '[]'::json)) > 0
                )
                THEN 'staff'
            WHEN lower(trim(coalesce(business_type, ''))) IN ('business', 'corporation', 'small_business', 'non_profit', 'government')
                THEN 'entity'
            ELSE 'individual'
        END::user_type
        """
    )

    op.alter_column(
        "users",
        "user_type",
        existing_type=user_type_enum,
        nullable=False,
        server_default="individual",
    )
    op.drop_column("users", "business_type")


def downgrade():
    op.add_column("users", sa.Column("business_type", sa.String(length=50), nullable=True))
    op.execute(
        """
        UPDATE users
        SET business_type = CASE
            WHEN user_type = 'entity' THEN 'business'
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
    op.drop_column("users", "user_type")

    user_type_enum = postgresql.ENUM(
        "individual",
        "entity",
        "staff",
        name="user_type",
        create_type=False,
    )
    user_type_enum.drop(op.get_bind(), checkfirst=True)
