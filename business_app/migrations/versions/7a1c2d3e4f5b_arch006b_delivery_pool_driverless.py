"""arch006b: a pool-status delivery must be driverless

Revision ID: 7a1c2d3e4f5b
Revises: 6f5d4c3b2a18
Create Date: 2026-06-11 17:00:00.000000

Defence-in-depth backstop for the reverse of
``ck_deliveries_person_required_after_assigned``: a delivery in a *pool* status
(scheduled/pending) must NOT retain a ``delivery_person_id``. Such "stranded"
rows are invisible to both the driver's active list (status filter excludes
scheduled/pending) and the unassigned pool (which only lists driverless rows),
so they silently fall out of every operational screen.

Mirrors ``assert_unassigned_for_pool_status`` in
business_app/utils/state_validators.py — keep both sides in sync.

Pre-flight: the migration first clears ``delivery_person_id`` on any existing
stranded rows (the same effect as returning them to the pool), prints what it
touched, re-checks inside the same transaction, and aborts before constraint
creation if anything still violates the invariant. The reconcile is idempotent
(only touches violating rows) so re-running is a no-op.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "7a1c2d3e4f5b"
down_revision = "6f5d4c3b2a18"
branch_labels = None
depends_on = None


# Mirrors DELIVERY_POOL_UNASSIGNED_STATES in state_validators.py. The delivery
# status column stores enum *values* (see Delivery.status values_callable).
DELIVERY_POOL_VALUES = ("scheduled", "pending")


def _quote_in_list(values):
    return ", ".join(f"'{v}'" for v in values)


def _reconcile_stranded(conn):
    """Clear the driver on any delivery stuck in a pool status, so the new CHECK
    constraint installs cleanly. This is the same repair as returning the
    delivery to the pool (StaffService.return_delivery_to_pool)."""
    pool_states = _quote_in_list(DELIVERY_POOL_VALUES)
    stranded = conn.execute(
        sa.text(
            f"""
        SELECT id, delivery_person_id, status
          FROM deliveries
         WHERE status IN ({pool_states})
           AND delivery_person_id IS NOT NULL
         ORDER BY id
        """
        )
    ).fetchall()

    if stranded:
        print(
            f"ARCH-006b reconcile: clearing delivery_person_id on {len(stranded)} "
            "stranded deliveries (pool status + assigned driver):"
        )
        for row in stranded[:50]:
            print(f"  delivery_id={row[0]} status={row[2]} driver={row[1]}")
        conn.execute(
            sa.text(
                f"""
            UPDATE deliveries
               SET delivery_person_id = NULL
             WHERE status IN ({pool_states})
               AND delivery_person_id IS NOT NULL
            """
            )
        )


def _abort_if_violations_exist():
    conn = op.get_bind()
    bad = conn.execute(
        sa.text(
            f"""
        SELECT COUNT(*) FROM deliveries
        WHERE status IN ({_quote_in_list(DELIVERY_POOL_VALUES)})
          AND delivery_person_id IS NOT NULL
        """
        )
    ).scalar()
    if bad:
        raise RuntimeError(
            f"ARCH-006b migration aborted: {bad} deliveries still violate the "
            "pool-status-must-be-driverless invariant after reconcile."
        )


def upgrade():
    conn = op.get_bind()
    _reconcile_stranded(conn)
    _abort_if_violations_exist()

    op.create_check_constraint(
        "ck_deliveries_no_driver_for_pool_status",
        "deliveries",
        f"status NOT IN ({_quote_in_list(DELIVERY_POOL_VALUES)}) OR delivery_person_id IS NULL",
    )


def downgrade():
    op.drop_constraint("ck_deliveries_no_driver_for_pool_status", "deliveries", type_="check")
