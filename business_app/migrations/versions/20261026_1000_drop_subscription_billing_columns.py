"""drop subscriptions.billing_cycle, delivery_frequency, next_billing_date, last_billing_date

Revision ID: 7c3d9e5a1b84
Revises: 5ea02acb0f0b
Create Date: 2026-10-26 10:00:00.000000

Spec: docs/superpowers/specs/2026-10-23-subscription-single-frequency-design.md §2.1, §4.1.
Plan: docs/superpowers/plans/2026-10-24-subscription-single-frequency.md, Task 6 (the CONTRACT step).
`frequency` and the DATE columns `next_delivery_date` / `last_delivery_date` (revision 5ea02acb0f0b) are
the only schedule now; no code reads the four old columns.

Upgrade: drop the index idx_subscriptions_status_next_billing, then the four columns. The
subscription_frequency type stays (`frequency` uses it).

Rollback strategy:
    downgrade() re-adds the four columns and the index: billing_cycle and delivery_frequency are both
    copied from `frequency`; next_billing_date is the Asia/Tashkent local midnight of next_delivery_date
    (now() for a row with no next_delivery_date, since the old column was NOT NULL); last_billing_date is
    the local midnight of last_delivery_date. LOSSY: the old values are not kept by the upgrade, so a
    pair that disagreed before 5ea02acb0f0b, and any time of day the old datetimes held, cannot come back.
    Safe to roll back only together with the code that wrote the old columns (plan Tasks 1-5).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "7c3d9e5a1b84"
down_revision = "5ea02acb0f0b"
branch_labels = None
depends_on = None

# Frozen copies: never import live constants into a migration.
LOCAL_TZ = "Asia/Tashkent"
INDEX_NAME = "idx_subscriptions_status_next_billing"
FREQUENCY_TYPE = postgresql.ENUM(
    "daily", "weekly", "biweekly", "monthly", name="subscription_frequency", create_type=False
)


def upgrade():
    op.execute(sa.text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))
    for column in ("billing_cycle", "delivery_frequency", "next_billing_date", "last_billing_date"):
        op.drop_column("subscriptions", column)


def downgrade():
    op.add_column("subscriptions", sa.Column("billing_cycle", FREQUENCY_TYPE, nullable=True))
    op.add_column("subscriptions", sa.Column("delivery_frequency", FREQUENCY_TYPE, nullable=True))
    op.add_column("subscriptions", sa.Column("next_billing_date", sa.DateTime(timezone=True), nullable=True))
    op.add_column("subscriptions", sa.Column("last_billing_date", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        sa.text(
            "UPDATE subscriptions SET billing_cycle = frequency, delivery_frequency = frequency, "
            f"next_billing_date = COALESCE((next_delivery_date::timestamp AT TIME ZONE '{LOCAL_TZ}'), now()), "
            f"last_billing_date = (last_delivery_date::timestamp AT TIME ZONE '{LOCAL_TZ}')"
        )
    )
    op.alter_column("subscriptions", "billing_cycle", nullable=False)
    op.alter_column("subscriptions", "delivery_frequency", nullable=False)
    op.alter_column("subscriptions", "next_billing_date", nullable=False)
    op.create_index(INDEX_NAME, "subscriptions", ["status", "next_billing_date"], unique=False)
