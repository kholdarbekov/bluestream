"""Add registration_method column to users table

Revision ID: d8f9e2a1b3c4
Revises: 76cc9da9c268
Create Date: 2025-01-06

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d8f9e2a1b3c4"
down_revision = "76cc9da9c268"
branch_labels = None
depends_on = None


def upgrade():
    # Add registration_method column to users table
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("registration_method", sa.String(20), server_default="email", nullable=False))
        batch_op.create_index("ix_users_registration_method", ["registration_method"])

    # Backfill existing users based on registration_source
    op.execute("UPDATE users SET registration_method = 'telegram' WHERE registration_source = 'telegram'")
    op.execute(
        "UPDATE users SET registration_method = 'email' WHERE registration_source = 'web' OR registration_source IS NULL"
    )


def downgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index("ix_users_registration_method")
        batch_op.drop_column("registration_method")
