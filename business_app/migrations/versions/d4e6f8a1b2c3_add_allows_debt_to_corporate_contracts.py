"""add allows debt flag to corporate contracts

Revision ID: d4e6f8a1b2c3
Revises: c1d8e4f7a2b3
Create Date: 2026-03-01 22:40:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d4e6f8a1b2c3"
down_revision = "c1d8e4f7a2b3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("corporate_contracts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "allows_debt",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_index(
            batch_op.f("ix_corporate_contracts_allows_debt"),
            ["allows_debt"],
            unique=False,
        )

    op.execute("UPDATE corporate_contracts SET allows_debt = FALSE " "WHERE allows_debt IS NULL")


def downgrade():
    with op.batch_alter_table("corporate_contracts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_corporate_contracts_allows_debt"))
        batch_op.drop_column("allows_debt")
