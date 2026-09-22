"""add an index on order_status_history (new_status, changed_at)

Revision ID: f1a2b3c4d5e6
Revises: d4e7f9a2b6c1
Create Date: 2026-09-14 10:00:00.000000

`orders` has no `delivered_at` column, so "when did this land" is the DELIVERED row in
`order_status_history`. `ReplenishmentService.delivered_qty` reads that table once per
product per stock check, and `rate_per_day` / `predicted_stockout_at` /
`last_delivered_qty` read it again per product; all of them filter on `new_status` and
window or order by `changed_at`, and only `order_id` was indexed.

Mirrored in business_app/models/order.py::OrderStatusHistory.__table_args__ so the
SQLite test schema (db.create_all()) carries the same index, and asserted by name
against the migrated schema in
tests/integration/test_sales_agent_pg_constraints.py.

Rollback strategy:
    downgrade() drops the index by name. Fully reversible: an index holds no data and
    no other migration references it.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "f1a2b3c4d5e6"
down_revision = "d4e7f9a2b6c1"
branch_labels = None
depends_on = None

INDEX_NAME = "idx_order_status_history_new_status_changed_at"


def upgrade():
    op.create_index(INDEX_NAME, "order_status_history", ["new_status", "changed_at"])


def downgrade():
    op.drop_index(INDEX_NAME, table_name="order_status_history")
