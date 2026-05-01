"""Add card verification tracking fields for Payme SMS OTP flow

Revision ID: a1b2c3d4e5f6
Revises: ef3ba1878e42
Create Date: 2024-12-24

This migration adds columns needed for Payme Subscribe API card verification:
- verification_attempts: Track failed OTP attempts (max 3)
- verification_code_sent_at: When the SMS code was sent
- verification_expires_at: When the SMS code expires
- masked_phone: Masked phone number from Payme (e.g., "99890*****31")
- payme_recurrent: Whether card supports recurring payments
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "ef3ba1878e42"
branch_labels = None
depends_on = None


def upgrade():
    # Add verification tracking columns to credit_cards table
    with op.batch_alter_table("credit_cards", schema=None) as batch_op:
        batch_op.add_column(sa.Column("verification_attempts", sa.Integer(), nullable=True, default=0))
        batch_op.add_column(sa.Column("verification_code_sent_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("verification_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("masked_phone", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("payme_recurrent", sa.Boolean(), nullable=True, default=False))

    # Set default values for existing rows
    op.execute("UPDATE credit_cards SET verification_attempts = 0 WHERE verification_attempts IS NULL")
    op.execute("UPDATE credit_cards SET payme_recurrent = FALSE WHERE payme_recurrent IS NULL")

    # Now make verification_attempts non-nullable with default
    with op.batch_alter_table("credit_cards", schema=None) as batch_op:
        batch_op.alter_column("verification_attempts", existing_type=sa.Integer(), nullable=False, server_default="0")
        batch_op.alter_column("payme_recurrent", existing_type=sa.Boolean(), nullable=False, server_default="false")


def downgrade():
    with op.batch_alter_table("credit_cards", schema=None) as batch_op:
        batch_op.drop_column("payme_recurrent")
        batch_op.drop_column("masked_phone")
        batch_op.drop_column("verification_expires_at")
        batch_op.drop_column("verification_code_sent_at")
        batch_op.drop_column("verification_attempts")
