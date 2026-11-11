"""add customer_credit_payouts.target_balance (prepayment return: the balance a "set balance" asked to leave)

Revision ID: c8a4f2d6b1e3
Revises: 5e2b9c71d4a8
Create Date: 2026-11-10 13:00:00.000000

Spec: docs/superpowers/specs/2026-11-10-prepayment-payout-design.md (P12), controller ruling of the final fix
wave: an idempotency key replays its payout only for the same return, and refuses a different one
(PAYOUT_IDEMPOTENCY_KEY_REUSED). A "set balance" return derives its amount under the locks, so its amount
cannot tell a replay from a request with another target; the target itself has to be kept.

Added:
  * customer_credit_payouts.target_balance   nullable NUMERIC(12, 2): the balance the admin asked to leave, or
                                             NULL for a return of a stated amount. CHECK >= 0 when set.

Rollback strategy:
    downgrade() drops the column and its CHECK. LOSSY only in that the recorded targets are discarded; the
    payouts themselves (amount, debits) are untouched and keep reconciling.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c8a4f2d6b1e3"
down_revision = "5e2b9c71d4a8"
branch_labels = None
depends_on = None

PAYOUTS = "customer_credit_payouts"
CHECK = f"ck_{PAYOUTS}_target_balance_nonneg"


def upgrade():
    op.add_column(PAYOUTS, sa.Column("target_balance", sa.Numeric(12, 2), nullable=True))
    op.create_check_constraint(CHECK, PAYOUTS, "target_balance IS NULL OR target_balance >= 0")


def downgrade():
    op.drop_constraint(CHECK, PAYOUTS, type_="check")
    op.drop_column(PAYOUTS, "target_balance")
