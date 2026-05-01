"""Add tax_committee_utilised_at to payment_fiscalizations

Revision ID: 3f8812d340db
Revises: 80105c879c6f
Create Date: 2026-04-09 12:14:25.018141

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "3f8812d340db"
down_revision = "80105c879c6f"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("payment_fiscalizations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tax_committee_utilised_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    with op.batch_alter_table("payment_fiscalizations", schema=None) as batch_op:
        batch_op.drop_column("tax_committee_utilised_at")
