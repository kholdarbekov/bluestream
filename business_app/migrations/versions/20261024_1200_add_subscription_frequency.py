"""add subscriptions.frequency; next/last_delivery_date become local DATEs

Revision ID: 5ea02acb0f0b
Revises: b4f1c7e9a2d3
Create Date: 2026-10-24 12:00:00.000000

Spec: docs/superpowers/specs/2026-10-23-subscription-single-frequency-design.md §2.6, §2.9, §4.1, §9.
Plan: docs/superpowers/plans/2026-10-24-subscription-single-frequency.md, Task 1 (the EXPAND step).
billing_cycle, delivery_frequency, next_billing_date and last_billing_date stay, and every writer keeps
them equal to the new columns until the contract migration (Task 6) drops them.

Upgrade, in one transaction:
  1. Pre-flight prints:
       * how many rows have billing_cycle != delivery_frequency. Their delivery frequency gives way to
         the cadence orders were really created on (billing_cycle, §2.6).
       * the weekly/biweekly rows whose delivery_day_of_week was not set by an admin create. The web
         builder stores the JS convention (0 = Sunday) and nothing on the row records which client set
         the day, so these are NOT converted; an operator checks them against the prod pre-flight SELECT.
  2. frequency (the existing subscription_frequency type, not re-created) := billing_cycle. A
     count-and-abort pre-flight, then NOT NULL.
  3. next_delivery_date and last_delivery_date: timestamptz -> DATE, the Asia/Tashkent local date.
  4. next_delivery_date := the first anchor date on or after next_billing_date's local date. The anchor
     rule is a FROZEN copy of business_app/services/subscription_schedule.py as of this revision; a
     migration must replay the same way forever. tests/integration/test_migrations_roundtrip.py pins the
     two equal on its fixture rows. A row with no possible anchor (a weekday outside 0-6) aborts the
     upgrade, naming it.
  5. Where the day is NULL, it is fixed from next_delivery_date (§2.9): the weekday for weekly and
     biweekly, the day of the month for monthly.
  6. last_delivery_date := the newest (by created_at) order of the subscription: its delivery_date, or
     the local date of its created_at. Rows with no order keep their converted value.

Rollback strategy:
    downgrade() drops `frequency` (the subscription_frequency type stays: billing_cycle and
    delivery_frequency use it) and turns the two DATE columns back into timestamptz at local midnight.
    next_billing_date is not touched in either direction: it still exists and every writer still fills
    it (local midnight of next_delivery_date).
    LOSSY: the time of day the two delivery-date columns held is gone; the recomputed
    next_delivery_date, the backfilled last_delivery_date and the days filled in by step 5 stay.
    Mismatched billing_cycle/delivery_frequency pairs are not changed by this migration, but a writer
    that saves a subscription after it collapses the pair to one frequency, and the old delivery
    frequency cannot be recovered. Safe to roll back while the code of Tasks 1-5 is rolled back with it.
"""

from calendar import monthrange
from datetime import timedelta

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "5ea02acb0f0b"
down_revision = "b4f1c7e9a2d3"
branch_labels = None
depends_on = None

# Frozen copies: never import live constants into a migration.
LOCAL_TZ = "Asia/Tashkent"
FREQUENCY_TYPE = postgresql.ENUM(
    "daily", "weekly", "biweekly", "monthly", name="subscription_frequency", create_type=False
)
DATE_COLUMNS = ("next_delivery_date", "last_delivery_date")
WEEKLY_KINDS = ("weekly", "biweekly")


def _print_preflight(conn):
    mismatched = conn.execute(
        sa.text("SELECT count(*) FROM subscriptions WHERE billing_cycle <> delivery_frequency")
    ).scalar()
    print(
        f"subscription frequency: {mismatched} subscription(s) have billing_cycle != delivery_frequency; "
        "frequency takes billing_cycle"
    )
    unmarked = (
        conn.execute(
            sa.text(
                "SELECT s.id FROM subscriptions s WHERE s.delivery_day_of_week IS NOT NULL "
                "AND s.billing_cycle IN ('weekly', 'biweekly') "
                "AND NOT EXISTS (SELECT 1 FROM subscription_logs l WHERE l.subscription_id = s.id "
                "AND l.action = 'created') ORDER BY s.id"
            )
        )
        .scalars()
        .all()
    )
    print(f"subscription frequency: delivery_day_of_week not set by an admin (may be 0 = Sunday): {list(unmarked)}")


def _add_frequency(conn):
    op.add_column("subscriptions", sa.Column("frequency", FREQUENCY_TYPE, nullable=True))
    op.execute(sa.text("UPDATE subscriptions SET frequency = billing_cycle"))
    missing = conn.execute(sa.text("SELECT id FROM subscriptions WHERE frequency IS NULL ORDER BY id")).scalars().all()
    if missing:
        raise RuntimeError(f"subscription frequency: rows without a billing_cycle to copy: {list(missing)}")
    op.alter_column("subscriptions", "frequency", existing_type=FREQUENCY_TYPE, nullable=False)


def _is_anchor(frequency, weekday, month_day, slot_active, slot_days, day):
    """Frozen copy of subscription_schedule's anchor rule (see the module docstring)."""
    if frequency in WEEKLY_KINDS:
        return weekday is None or day.weekday() == weekday
    if frequency == "monthly":
        return month_day is None or day.day == min(month_day, monthrange(day.year, day.month)[1])
    weekdays = {d for d in (slot_days or ()) if d in range(7)} if slot_active else set()
    return not weekdays or day.weekday() in weekdays


def _backfill_next_delivery_and_days(conn):
    rows = conn.execute(
        sa.text(
            "SELECT s.id, s.frequency::text, s.delivery_day_of_week, s.delivery_day_of_month, t.is_active, "
            f"t.available_days, (s.next_billing_date AT TIME ZONE '{LOCAL_TZ}')::date "
            "FROM subscriptions s LEFT JOIN delivery_time_slots t ON t.id = s.delivery_time_slot_id ORDER BY s.id"
        )
    ).all()
    unschedulable = []
    for sub_id, frequency, weekday, month_day, slot_active, slot_days, billing_day in rows:
        candidates = (billing_day + timedelta(days=offset) for offset in range(31))
        next_day = next(
            (d for d in candidates if _is_anchor(frequency, weekday, month_day, slot_active, slot_days, d)), None
        )
        if next_day is None:
            unschedulable.append(sub_id)
            continue
        conn.execute(
            sa.text(
                "UPDATE subscriptions SET next_delivery_date = :day, "
                "delivery_day_of_week = CASE WHEN frequency::text IN ('weekly', 'biweekly') "
                "THEN COALESCE(delivery_day_of_week, :weekday) ELSE delivery_day_of_week END, "
                "delivery_day_of_month = CASE WHEN frequency::text = 'monthly' "
                "THEN COALESCE(delivery_day_of_month, :month_day) ELSE delivery_day_of_month END "
                "WHERE id = :id"
            ),
            {"day": next_day, "weekday": next_day.weekday(), "month_day": next_day.day, "id": sub_id},
        )
    if unschedulable:
        raise RuntimeError(
            f"subscription frequency: no delivery date can be scheduled for subscription(s) {unschedulable} "
            "(delivery_day_of_week must be 0-6, delivery_day_of_month 1-31); fix them and rerun"
        )


def upgrade():
    conn = op.get_bind()
    _print_preflight(conn)
    _add_frequency(conn)
    for column in DATE_COLUMNS:
        op.execute(
            sa.text(
                f"ALTER TABLE subscriptions ALTER COLUMN {column} TYPE DATE "
                f"USING ({column} AT TIME ZONE '{LOCAL_TZ}')::date"
            )
        )
    _backfill_next_delivery_and_days(conn)
    op.execute(
        sa.text(
            "UPDATE subscriptions s SET last_delivery_date = newest.day FROM ("
            "  SELECT DISTINCT ON (o.subscription_id) o.subscription_id, "
            f"   COALESCE(o.delivery_date, (o.created_at AT TIME ZONE '{LOCAL_TZ}')::date) AS day "
            "  FROM orders o WHERE o.subscription_id IS NOT NULL "
            "  ORDER BY o.subscription_id, o.created_at DESC, o.id DESC"
            ") newest WHERE newest.subscription_id = s.id"
        )
    )


def downgrade():
    for column in DATE_COLUMNS:
        op.execute(
            sa.text(
                f"ALTER TABLE subscriptions ALTER COLUMN {column} TYPE TIMESTAMP WITH TIME ZONE "
                f"USING ({column}::timestamp AT TIME ZONE '{LOCAL_TZ}')"
            )
        )
    op.drop_column("subscriptions", "frequency")
