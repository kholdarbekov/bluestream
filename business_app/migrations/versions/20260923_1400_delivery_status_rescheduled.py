"""a held delivery: the `rescheduled` delivery status and its driverless CHECK

Revision ID: d7e8f9a0b1c2
Revises: f2a3b4c5d6e7
Create Date: 2026-09-23 14:00:00.000000

R3 of docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md. A delivery that was
released to drivers once and has been moved to a later day waits in `rescheduled` until its new
release time. It never has a driver, so `ck_deliveries_no_driver_for_pool_status` widens from the
claimable pool (scheduled/pending, 7a1c2d3e4f5b) to every driverless status. The CHECK mirrors
`DELIVERY_DRIVERLESS_STATES` in business_app/utils/state_validators.py; keep both in sync.
tests/integration/test_delivery_rescheduled_pg_e2e.py fails if they drift.

No reconcile: no row can hold the new value before this runs, and the scheduled/pending half of
the CHECK is the one already in force.

Rollback strategy:
    downgrade() refuses while any delivery, or any delivery_status_history row, still says
    `rescheduled`, because the previous code can load neither. Otherwise it restores the
    scheduled/pending CHECK. Postgres cannot drop an enum value, so `rescheduled` stays in the
    type, which is harmless (as with 'force_closed' in f5b2c8d41a7e).
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "d7e8f9a0b1c2"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None

CHECK_NAME = "ck_deliveries_no_driver_for_pool_status"

# Mirrors DELIVERY_DRIVERLESS_STATES. The status column stores enum VALUES.
DRIVERLESS_VALUES = ("scheduled", "pending", "rescheduled")
# What 7a1c2d3e4f5b installed: DELIVERY_POOL_UNASSIGNED_STATES, the claimable pool.
POOL_VALUES = ("scheduled", "pending")


def _driverless_check(values):
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"status NOT IN ({quoted}) OR delivery_person_id IS NULL"


def upgrade():
    # Postgres refuses a new enum value inside the transaction that added it ("unsafe use of new
    # value"), and the CHECK below names it, so the ADD VALUE is committed on its own first.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE delivery_status ADD VALUE IF NOT EXISTS 'rescheduled'")

    op.drop_constraint(CHECK_NAME, "deliveries", type_="check")
    op.create_check_constraint(CHECK_NAME, "deliveries", _driverless_check(DRIVERLESS_VALUES))


def downgrade():
    conn = op.get_bind()
    held = conn.execute(sa.text("SELECT COUNT(*) FROM deliveries WHERE status::text = 'rescheduled'")).scalar()
    history = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM delivery_status_history "
            "WHERE old_status::text = 'rescheduled' OR new_status::text = 'rescheduled'"
        )
    ).scalar()
    if held or history:
        raise RuntimeError(
            f"Downgrade of {revision} refused: {held} deliveries and {history} delivery_status_history "
            "rows still say 'rescheduled', and the previous code can load neither. Move those rows off "
            "the status by hand before rolling back."
        )

    op.drop_constraint(CHECK_NAME, "deliveries", type_="check")
    op.create_check_constraint(CHECK_NAME, "deliveries", _driverless_check(POOL_VALUES))
