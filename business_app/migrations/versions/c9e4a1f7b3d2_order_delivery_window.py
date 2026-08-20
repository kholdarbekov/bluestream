"""Replace orders.delivery_time_slot with an open-ended delivery window

`delivery_time_slot` was a free-text String(20) ("09:00-12:00") that could not
express "until 10:00" or "after 19:00", and two call sites DECIDED from the
string rather than merely displaying it. It is replaced by a nullable
(start, end) time pair where either side may be open.

`delivery_date` becomes a DATE. It is a calendar day, not an instant; as a
timestamp it re-opens the local-midnight-vs-UTC-midnight bug class that already
broke driver route day boundaries once (05:00 Tashkent skew).

Both columns are NULL on every row in dev (137/137) and, per
DispatchService's module docstring, on every active order in production. The
backfill below is defensive only.

Revision ID: c9e4a1f7b3d2
Revises: a4c7e1b9d3f5
Create Date: 2026-08-19 20:10:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "c9e4a1f7b3d2"
down_revision = "a4c7e1b9d3f5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("delivery_window_start", sa.Time(), nullable=True))
    op.add_column("orders", sa.Column("delivery_window_end", sa.Time(), nullable=True))

    # Defensive backfill: parse any legacy "HH:MM-HH:MM" into the window pair
    # before the column is dropped. Rows that do not match are left NULL rather
    # than guessed at.
    op.execute(
        """
        UPDATE orders
           SET delivery_window_start = split_part(delivery_time_slot, '-', 1)::time,
               delivery_window_end   = split_part(delivery_time_slot, '-', 2)::time
         WHERE delivery_time_slot ~ '^[0-9]{2}:[0-9]{2}-[0-9]{2}:[0-9]{2}$'
        """
    )

    op.drop_index("idx_orders_delivery_slot_date", table_name="orders")
    op.drop_column("orders", "delivery_time_slot")

    op.alter_column(
        "orders",
        "delivery_date",
        type_=sa.Date(),
        existing_type=sa.DateTime(timezone=True),
        postgresql_using="delivery_date::date",
        existing_nullable=True,
    )
    op.create_index("idx_orders_delivery_date", "orders", ["delivery_date"], unique=False)


def downgrade():
    op.drop_index("idx_orders_delivery_date", table_name="orders")
    op.alter_column(
        "orders",
        "delivery_date",
        type_=sa.DateTime(timezone=True),
        existing_type=sa.Date(),
        postgresql_using="delivery_date::timestamptz",
        existing_nullable=True,
    )
    op.add_column("orders", sa.Column("delivery_time_slot", sa.String(length=20), nullable=True))
    op.execute(
        """
        UPDATE orders
           SET delivery_time_slot = to_char(delivery_window_start, 'HH24:MI')
                                 || '-' || to_char(delivery_window_end, 'HH24:MI')
         WHERE delivery_window_start IS NOT NULL
           AND delivery_window_end IS NOT NULL
        """
    )
    op.create_index("idx_orders_delivery_slot_date", "orders", ["delivery_time_slot", "delivery_date"], unique=False)
    op.drop_column("orders", "delivery_window_end")
    op.drop_column("orders", "delivery_window_start")
