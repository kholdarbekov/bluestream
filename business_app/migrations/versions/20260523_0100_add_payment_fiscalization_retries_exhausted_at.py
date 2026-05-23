"""Add retries_exhausted_at to payment_fiscalizations

Revision ID: b9f3d2a4c6e8
Revises: a8e2c1f3b5d6
Create Date: 2026-05-23 01:00:00.000000

Adds payment_fiscalizations.retries_exhausted_at, a nullable timestamp set
exactly when the Click fiscalization Celery task catches
MaxRetriesExceededError. NULL means "no admin attention needed."

Backfills historical rows that were already terminally failed (status='failed'
AND attempts >= 4) by setting retries_exhausted_at = last_attempt_at, so they
surface on the new admin "Fiscalization Failures" page after deploy.
"""

from alembic import op
import sqlalchemy as sa


revision = "b9f3d2a4c6e8"
down_revision = "a8e2c1f3b5d6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("payment_fiscalizations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("retries_exhausted_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(
            "idx_payment_fiscalizations_retries_exhausted",
            ["retries_exhausted_at"],
            unique=False,
        )

    op.execute(
        """
        UPDATE payment_fiscalizations
        SET retries_exhausted_at = last_attempt_at
        WHERE status = 'failed'
          AND attempts >= 4
          AND retries_exhausted_at IS NULL
          AND last_attempt_at IS NOT NULL
        """
    )


def downgrade():
    with op.batch_alter_table("payment_fiscalizations", schema=None) as batch_op:
        batch_op.drop_index("idx_payment_fiscalizations_retries_exhausted")
        batch_op.drop_column("retries_exhausted_at")
