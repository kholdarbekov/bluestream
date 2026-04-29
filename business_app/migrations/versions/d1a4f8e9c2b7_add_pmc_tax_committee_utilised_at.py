"""Add tax_committee_utilised_at to product_marking_codes for proactive Asl Belgisi pre-registration

Revision ID: d1a4f8e9c2b7
Revises: b8e3c9f5d2a4
Create Date: 2026-04-28 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d1a4f8e9c2b7"
down_revision = "b8e3c9f5d2a4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("product_marking_codes", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tax_committee_utilised_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.create_index(
            "ix_product_marking_codes_tax_committee_utilised_at",
            ["tax_committee_utilised_at"],
            unique=False,
        )
        batch_op.create_index(
            "idx_pmc_product_status_preutil",
            ["product_id", "status", "tax_committee_utilised_at"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table("product_marking_codes", schema=None) as batch_op:
        batch_op.drop_index("idx_pmc_product_status_preutil")
        batch_op.drop_index("ix_product_marking_codes_tax_committee_utilised_at")
        batch_op.drop_column("tax_committee_utilised_at")
