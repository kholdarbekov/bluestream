"""add orders.online_payable_since: when the order became payable on Click

Revision ID: 3f8b2d6a9c41
Revises: 7c3d9e5a1b84
Create Date: 2026-10-27 12:00:00.000000

Spec: docs/superpowers/specs/2026-10-27-bot-web-parity-design.md §4.4, §14, §17 Q3.
Plan: docs/superpowers/plans/2026-10-27-bot-web-parity.md, Task 3.

An unpaid online order's payment deadline is 24 hours from when it became payable on Click, no longer from its
creation. `PaymentService.open_click_payment_window` stamps that moment: at a checkout on an online rail, and when
a customer's Pay or staff move the order onto Click.

Upgrade: add the nullable column, then stamp the deploy time (`now()`, one value for the whole migration) on every
order still unpaid on an online rail (click, card, payme) that is not cancelled or returned. Each of those gets a
full 24-hour window from the deploy, so nobody is told their order was "not paid by" a deadline dated days ago.
Every other row stays NULL, and `Order.payment_window_start` falls back to its `created_at`.

Rollback strategy:
    downgrade() drops the column. LOSSLESS for every value this migration wrote: the column is new and nothing
    else holds it. Stamps written after the deploy are discarded, so the old code's deadline, counted from
    `created_at`, applies again: an order moved onto Click days after it was placed is then past its deadline at
    once. Roll back only together with the code that reads the column.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "3f8b2d6a9c41"
down_revision = "7c3d9e5a1b84"
branch_labels = None
depends_on = None

# The rails and statuses are frozen literals: never import live constants into a migration.
BACKFILL = (
    "UPDATE orders SET online_payable_since = now() "
    "WHERE is_paid IS NOT TRUE "
    "AND payment_method IN ('click', 'card', 'payme') "
    "AND status NOT IN ('cancelled', 'returned')"
)


def upgrade():
    op.add_column("orders", sa.Column("online_payable_since", sa.DateTime(timezone=True), nullable=True))
    op.execute(sa.text(BACKFILL))


def downgrade():
    op.drop_column("orders", "online_payable_since")
