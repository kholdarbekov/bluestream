"""add loyalty eligibility flag to corporate contracts

Revision ID: c1d8e4f7a2b3
Revises: b5a4f0d9c3e1
Create Date: 2026-03-01 21:10:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c1d8e4f7a2b3"
down_revision = "b5a4f0d9c3e1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("corporate_contracts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_loyalty_points_eligible",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_index(
            batch_op.f("ix_corporate_contracts_is_loyalty_points_eligible"),
            ["is_loyalty_points_eligible"],
            unique=False,
        )

    op.execute(
        "UPDATE corporate_contracts SET is_loyalty_points_eligible = FALSE " "WHERE is_loyalty_points_eligible IS NULL"
    )


def downgrade():
    with op.batch_alter_table("corporate_contracts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_corporate_contracts_is_loyalty_points_eligible"))
        batch_op.drop_column("is_loyalty_points_eligible")
