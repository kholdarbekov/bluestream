"""add cod_debt_check_exempt to users

Adds:
  - users.cod_debt_check_exempt BOOLEAN NOT NULL DEFAULT FALSE.

Flag that lets an admin grant a permanent exemption from the active-COD-debt
cap enforced in CashCollectionService.is_customer_cod_restricted(). Reserved
for trusted customers (close partners, relatives) who must always be allowed
to order cash-on-delivery regardless of outstanding debts. Mirrors the
existing grocery-store exemption.

Existing rows backfill to FALSE via ``server_default="false"``; without it
the SQLAlchemy ``default=False`` only fires on ORM-insert and existing rows
would violate the NOT NULL constraint. The server default is kept permanently
as a safety net.

On Postgres >= 11 ``ADD COLUMN ... NOT NULL DEFAULT false`` is a
metadata-only operation -- no table rewrite -- so this is safe on the live
users table. No index added: low-cardinality and only ever read via PK
lookup on User.

Revision ID: b9d1e3f7a2c5
Revises: a3f7c8b1d2e9
Create Date: 2026-05-11 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b9d1e3f7a2c5"
down_revision = "a3f7c8b1d2e9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "cod_debt_check_exempt",
                sa.Boolean(),
                nullable=False,
                server_default="false",
            )
        )


def downgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("cod_debt_check_exempt")
