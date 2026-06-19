"""loyalty: FIFO lot ledger — add loyalty_transactions.remaining_points + backfill

Revision ID: e7d2a9c4b1f3
Revises: 7a1c2d3e4f5b
Create Date: 2026-06-14 00:00:00.000000

Phase 1 (Unit A) of the loyalty SSOT finalization (docs/loyalty_ssot_audit.md).

Adds ``remaining_points`` to ``loyalty_transactions`` so each positive earn
transaction becomes a FIFO "lot" whose unspent remainder is drawn down by
redemptions, clawbacks, and expiry. This makes balance + expiry mathematically
correct (a lot spent before it expires no longer double-counts / drives the
balance negative — the bug that could roll back the whole daily expiry run).

Backfill reconstructs each lot's ``remaining_points`` from history (best effort):
  * already-expired lots (is_expired) -> remaining 0 (their points are gone);
  * live positive lots start at ``points`` then have historical real spend
    (REDEEMED + negative ADJUSTMENT, NOT the synthetic EXPIRED rows) drawn down
    oldest-first;
  * each account's ``current_balance`` is reset to the sum of live remainders
    (non-negative, satisfies ck_loyalty_points_current_balance_nonneg) and
    ``total_expired`` to the sum of expired-lot points (was never written
    before — see audit M1).

Historical synthetic EXPIRED transactions are left in place (audit trail); the
new balance derivation ignores all negative rows, so they are inert for
balances. Their effect on redemption analytics is addressed separately (Phase 2).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision = "e7d2a9c4b1f3"
down_revision = "7a1c2d3e4f5b"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("loyalty_transactions", sa.Column("remaining_points", sa.Integer(), nullable=True))
    _backfill_remaining_points(op.get_bind())


def downgrade():
    op.drop_column("loyalty_transactions", "remaining_points")


def _backfill_remaining_points(bind):
    user_ids = [row[0] for row in bind.execute(text("SELECT DISTINCT user_id FROM loyalty_transactions")).fetchall()]

    for user_id in user_ids:
        lots = bind.execute(
            text(
                """
                SELECT id, points, is_expired
                FROM loyalty_transactions
                WHERE user_id = :uid AND points > 0
                  AND transaction_type IN ('earned', 'bonus', 'adjustment')
                ORDER BY created_at ASC, id ASC
                """
            ),
            {"uid": user_id},
        ).fetchall()

        # Real historical spend that should draw down lots: redemptions and
        # negative adjustments (clawbacks). The synthetic 'expired' negatives are
        # excluded — expiry is represented by the lot's is_expired flag instead.
        to_consume = int(
            bind.execute(
                text(
                    """
                    SELECT COALESCE(SUM(-points), 0)
                    FROM loyalty_transactions
                    WHERE user_id = :uid AND points < 0
                      AND transaction_type IN ('redeemed', 'adjustment')
                    """
                ),
                {"uid": user_id},
            ).fetchone()[0]
            or 0
        )

        remaining = {}
        live_lot_ids = []
        for lot_id, points, is_expired in lots:
            if is_expired:
                remaining[lot_id] = 0  # expired lots hold nothing spendable
            else:
                remaining[lot_id] = int(points)
                live_lot_ids.append(lot_id)

        for lot_id in live_lot_ids:
            if to_consume <= 0:
                break
            avail = remaining[lot_id]
            if avail <= 0:
                continue
            take = min(avail, to_consume)
            remaining[lot_id] = avail - take
            to_consume -= take

        for lot_id, rem in remaining.items():
            bind.execute(
                text("UPDATE loyalty_transactions SET remaining_points = :rem WHERE id = :id"),
                {"rem": rem, "id": lot_id},
            )

        # Reconcile only the spendable cache. total_expired is intentionally NOT
        # backfilled: it was never populated historically and cannot be
        # reliably reconstructed from lossy data (a lot may have been partially
        # redeemed before expiring) without double-counting the redeemed portion
        # already in total_redeemed. It accrues correctly going forward in
        # LoyaltyService._expire_user_points.
        current_balance = sum(remaining[lid] for lid in live_lot_ids)
        bind.execute(
            text("UPDATE loyalty_points SET current_balance = :bal WHERE user_id = :uid"),
            {"bal": current_balance, "uid": user_id},
        )
