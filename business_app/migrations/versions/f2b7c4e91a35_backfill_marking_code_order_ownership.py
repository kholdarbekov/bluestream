"""Backfill product_marking_codes.order_id from the allocation ledger

Data-only. Required by the marking-code ownership SSOT
(`PaymentFiscalizationService._codes_currently_held`), which decides "does this
payment still hold this code" partly on `product_marking_codes.order_id`.

Two writers reserve a code, and until now only one of them stamped ownership:

* `reserve_required_marking_codes` sets `order_id` (correct), and
* `_precheck_and_replace_invalid_codes` substituted a replacement code setting
  only `status` and `reserved_at`, leaving `order_id` NULL.

Those NULL rows are invisible to the ownership conjunct, so on the day it ships
they would have produced a short fiscal payload ("Expected N labels ... got
N-1"), a code that `release_reserved_marking_codes` skips forever (leaking pool
inventory that `sync_stock_from_marking_codes` counts as unavailable), and a
code `mark_reserved_codes_used` never flips to used. The substitution path is
fixed in the same change; this migration repairs the rows it already wrote.

Measured on dev `bluestream_db` before writing this: 10 affected rows, all 10
attributable to exactly one order via the ledger, 0 ambiguous. Run the probe in
`_probe()` against production before upgrading — if it reports any ambiguous
row, STOP and resolve those by hand rather than letting `max(order_id)` pick.

Idempotent: re-running is `UPDATE 0`.

Revision ID: f2b7c4e91a35
Revises: c9e4a1f7b3d2
Create Date: 2026-08-23 17:05:00.000000

"""

from alembic import op

revision = "f2b7c4e91a35"
down_revision = "c9e4a1f7b3d2"
branch_labels = None
depends_on = None


# Read-only. Run this against production BEFORE `flask db upgrade`:
#   docker compose exec -T postgres psql -U postgres -d bluestream_db -c "<_PROBE_SQL>"
_PROBE_SQL = """
SELECT c.status::text AS status,
       count(*) AS unattributed,
       count(*) FILTER (WHERE a.order_id IS NOT NULL) AS recoverable,
       count(*) FILTER (WHERE a.n_orders > 1) AS ambiguous
  FROM product_marking_codes c
  LEFT JOIN LATERAL (
       SELECT max(x.order_id) AS order_id, count(DISTINCT x.order_id) AS n_orders
         FROM order_item_marking_code_allocations x
        WHERE x.product_marking_code_id = c.id
          AND x.action = 'reserved'
  ) a ON true
 WHERE c.order_id IS NULL
   AND c.status IN ('reserved', 'used')
 GROUP BY 1;
"""


def upgrade():
    # Only rows the ledger attributes to EXACTLY ONE order are touched. An
    # ambiguous code (reserved against two different orders over its life) is
    # deliberately left NULL for a human to resolve — guessing here would put
    # the wrong label on a tax receipt.
    op.execute(
        """
        UPDATE product_marking_codes c
           SET order_id = src.order_id,
               updated_at = now()
          FROM (
                SELECT x.product_marking_code_id AS code_id,
                       min(x.order_id) AS order_id
                  FROM order_item_marking_code_allocations x
                 WHERE x.action = 'reserved'
                 GROUP BY x.product_marking_code_id
                HAVING count(DISTINCT x.order_id) = 1
               ) AS src
         WHERE src.code_id = c.id
           AND c.order_id IS NULL
           AND c.status IN ('reserved', 'used')
        """
    )


def downgrade():
    # Deliberately not reversible. Clearing order_id again would re-create the
    # unattributed rows this migration exists to remove, and we cannot tell
    # which rows were NULL before it ran from any surviving marker.
    pass
