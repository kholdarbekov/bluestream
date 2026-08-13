"""driver location accuracy

Revision ID: b6c4e8a2f7d1
Revises: a4e7c2f9b1d8
Create Date: 2026-08-13

Spec §5.1. Additive and nullable: existing rows keep NULL, which the accept
rule treats as "client did not report", not as "coarse".
"""

from alembic import op
import sqlalchemy as sa

revision = "b6c4e8a2f7d1"
down_revision = "a4e7c2f9b1d8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "delivery_persons",
        sa.Column("location_accuracy_m", sa.Float(), nullable=True),
    )


def downgrade():
    op.drop_column("delivery_persons", "location_accuracy_m")
