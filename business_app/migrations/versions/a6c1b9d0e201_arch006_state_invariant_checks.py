"""arch006: CHECK constraints for required FKs at terminal states

Revision ID: a6c1b9d0e201
Revises: c4e7b9a8f102
Create Date: 2026-04-25 12:00:00.000000

Adds defence-in-depth CHECK constraints that mirror the service-layer guards
introduced in business_app/utils/state_validators.py.

Pre-flight: run ``python -m scripts.audit_arch006_data`` first to inspect
violations. The migration itself reconciles legacy rows in-place per the
rules confirmed with the data owner and then re-runs the same probes inside
the same transaction; if anything still violates the invariants (e.g. a user
with no address at all) the migration aborts before constraint creation so
nothing partial lands.

Constraints added:
  * orders.ck_orders_address_required_after_pending
      status NOT IN delivery-bearing states OR delivery_address_id IS NOT NULL
  * orders.ck_orders_staff_creator_for_staff_source
      order_source NOT IN ('phone','admin') OR created_by_staff_id IS NOT NULL
  * deliveries.ck_deliveries_person_required_after_assigned
      status NOT IN active states OR delivery_person_id IS NOT NULL
  * payments.ck_payments_cash_completed_requires_collector
      payment_method != 'cash' OR status != 'completed' OR collected_by IS NOT NULL
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a6c1b9d0e201"
down_revision = "c4e7b9a8f102"
branch_labels = None
depends_on = None


# Mirrors business_app/utils/state_validators.py. Keep in sync if either side
# of the invariant moves.
ORDER_REQUIRES_ADDRESS_VALUES = (
    "confirmed",
    "preparing",
    "out_for_delivery",
    "delivered",
    "returned",
)
DELIVERY_REQUIRES_PERSON_VALUES = (
    "assigned",
    "picked_up",
    "in_transit",
    "arrived",
    "delivered",
)
STAFF_ORDER_SOURCES = ("phone", "admin")


def _quote_in_list(values):
    return ", ".join(f"'{v}'" for v in values)


def _reconcile_legacy_violations(conn):
    """Backfill the four FK invariants on legacy rows so the new CHECK
    constraints install cleanly.

    Was previously a separate ``scripts/reconcile_arch006_data.sql`` file;
    merged in here so the migration is self-contained and the reconcile +
    constraint install run inside one Alembic transaction. Idempotent: each
    update only touches NULL rows; re-running the migration after a prior
    successful reconcile is a no-op.

    Backfill rules (confirmed with the data owner):
      1. orders.delivery_address_id    ← owning user's default address
                                         (fallback: oldest address by id)
      2. orders.created_by_staff_id    ← any single user with role='admin'
      3. deliveries.delivery_person_id ← any driver (delivery_persons.user_id)
      4. payments.collected_by         ← any driver (delivery_persons.user_id)

    Driver identity is sourced from the ``delivery_persons`` profile table —
    ``users.role`` has drifted historically (some staff still carry
    role='customer'); ``delivery_persons.user_id`` is the canonical join.

    Backfill source (admin user / driver) is looked up only when there are
    rows that actually need it. A fresh-install database with no legacy rows
    skips the lookup entirely, so the migration runs against a brand-new DB
    without requiring seed data.
    """
    address_states = _quote_in_list(ORDER_REQUIRES_ADDRESS_VALUES)
    staff_sources = _quote_in_list(STAFF_ORDER_SOURCES)
    delivery_states = _quote_in_list(DELIVERY_REQUIRES_PERSON_VALUES)

    # 1) orders.delivery_address_id ← user's default-or-oldest address
    orders_missing_address = conn.execute(
        sa.text(
            f"""
        SELECT COUNT(*) FROM orders
        WHERE delivery_address_id IS NULL
          AND status IN ({address_states})
        """
        )
    ).scalar()

    if orders_missing_address:
        print(
            f"ARCH-006 reconcile: backfilling {orders_missing_address} "
            "orders.delivery_address_id from each user's default/oldest address"
        )
        conn.execute(
            sa.text(
                f"""
            WITH chosen_address AS (
                SELECT DISTINCT ON (user_id)
                       user_id,
                       id AS address_id
                  FROM addresses
                 ORDER BY user_id,
                          is_default DESC NULLS LAST,
                          id ASC
            )
            UPDATE orders o
               SET delivery_address_id = ca.address_id
              FROM chosen_address ca
             WHERE o.user_id = ca.user_id
               AND o.delivery_address_id IS NULL
               AND o.status IN ({address_states})
            """
            )
        )

    # 2) orders.created_by_staff_id ← any admin user
    orders_missing_creator = conn.execute(
        sa.text(
            f"""
        SELECT COUNT(*) FROM orders
        WHERE created_by_staff_id IS NULL
          AND order_source IN ({staff_sources})
        """
        )
    ).scalar()

    if orders_missing_creator:
        admin_id = conn.execute(sa.text("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")).scalar()
        if admin_id is None:
            raise RuntimeError(
                f"ARCH-006 reconcile: {orders_missing_creator} orders need a "
                "staff creator backfill but no users with role='admin' exist. "
                "Create an admin user before re-running the migration."
            )
        print(
            f"ARCH-006 reconcile: backfilling {orders_missing_creator} "
            f"orders.created_by_staff_id with admin user id={admin_id}"
        )
        conn.execute(
            sa.text(
                f"""
                UPDATE orders
                   SET created_by_staff_id = :admin_id
                 WHERE created_by_staff_id IS NULL
                   AND order_source IN ({staff_sources})
                """
            ),
            {"admin_id": admin_id},
        )

    # 3) deliveries.delivery_person_id ← any driver
    deliveries_missing_person = conn.execute(
        sa.text(
            f"""
        SELECT COUNT(*) FROM deliveries
        WHERE delivery_person_id IS NULL
          AND status IN ({delivery_states})
        """
        )
    ).scalar()

    if deliveries_missing_person:
        driver_id = conn.execute(sa.text("SELECT user_id FROM delivery_persons ORDER BY user_id LIMIT 1")).scalar()
        if driver_id is None:
            raise RuntimeError(
                f"ARCH-006 reconcile: {deliveries_missing_person} deliveries "
                "need a delivery_person backfill but delivery_persons is empty. "
                "Create a delivery driver before re-running the migration."
            )
        print(
            f"ARCH-006 reconcile: backfilling {deliveries_missing_person} "
            f"deliveries.delivery_person_id with driver user id={driver_id}"
        )
        conn.execute(
            sa.text(
                f"""
                UPDATE deliveries
                   SET delivery_person_id = :driver_id
                 WHERE delivery_person_id IS NULL
                   AND status IN ({delivery_states})
                """
            ),
            {"driver_id": driver_id},
        )

    # 4) payments.collected_by ← any driver (cash + completed only)
    cash_payments_missing_collector = conn.execute(
        sa.text(
            """
        SELECT COUNT(*) FROM payments
        WHERE collected_by IS NULL
          AND payment_method = 'cash'
          AND status = 'completed'
        """
        )
    ).scalar()

    if cash_payments_missing_collector:
        driver_id = conn.execute(sa.text("SELECT user_id FROM delivery_persons ORDER BY user_id LIMIT 1")).scalar()
        if driver_id is None:
            raise RuntimeError(
                f"ARCH-006 reconcile: {cash_payments_missing_collector} "
                "completed cash payments need a collector backfill but "
                "delivery_persons is empty. Create a delivery driver before "
                "re-running the migration."
            )
        print(
            f"ARCH-006 reconcile: backfilling {cash_payments_missing_collector} "
            f"payments.collected_by with driver user id={driver_id}"
        )
        conn.execute(
            sa.text(
                """
                UPDATE payments
                   SET collected_by = :driver_id
                 WHERE collected_by IS NULL
                   AND payment_method = 'cash'
                   AND status = 'completed'
                """
            ),
            {"driver_id": driver_id},
        )


def _abort_if_violations_exist():
    """Refuse to install constraints if rows still violate after reconcile.

    Reaches this point only if ``_reconcile_legacy_violations`` couldn't fix
    every row — typically because the owning user has no addresses at all
    (rule 1 has no per-user backfill source). Surface those orphans clearly
    so an operator can either delete them, transition them back to a pre-
    delivery state, or attach an address manually.
    """
    conn = op.get_bind()

    bad_orders_address = conn.execute(
        sa.text(
            f"""
        SELECT COUNT(*) FROM orders
        WHERE status IN ({_quote_in_list(ORDER_REQUIRES_ADDRESS_VALUES)})
          AND delivery_address_id IS NULL
        """
        )
    ).scalar()

    bad_orders_creator = conn.execute(
        sa.text(
            f"""
        SELECT COUNT(*) FROM orders
        WHERE order_source IN ({_quote_in_list(STAFF_ORDER_SOURCES)})
          AND created_by_staff_id IS NULL
        """
        )
    ).scalar()

    bad_deliveries = conn.execute(
        sa.text(
            f"""
        SELECT COUNT(*) FROM deliveries
        WHERE status IN ({_quote_in_list(DELIVERY_REQUIRES_PERSON_VALUES)})
          AND delivery_person_id IS NULL
        """
        )
    ).scalar()

    bad_cash_payments = conn.execute(
        sa.text(
            """
        SELECT COUNT(*) FROM payments
        WHERE payment_method = 'cash'
          AND status = 'completed'
          AND collected_by IS NULL
        """
        )
    ).scalar()

    violations = {
        "orders_missing_address": bad_orders_address,
        "orders_missing_creator": bad_orders_creator,
        "deliveries_missing_person": bad_deliveries,
        "cash_payments_missing_collector": bad_cash_payments,
    }
    if any(violations.values()):
        # Surface specific orphan order ids for rule 1 — most common case
        # the auto-reconcile can't fix (user has no addresses).
        if bad_orders_address:
            orphan_orders = conn.execute(
                sa.text(
                    f"""
                SELECT id, user_id, status, order_source
                  FROM orders
                 WHERE status IN ({_quote_in_list(ORDER_REQUIRES_ADDRESS_VALUES)})
                   AND delivery_address_id IS NULL
                 ORDER BY id
                 LIMIT 20
                """
                )
            ).fetchall()
            print("ARCH-006: orders still missing delivery_address_id (first 20):")
            for row in orphan_orders:
                print(f"  order_id={row[0]} user_id={row[1]} status={row[2]} source={row[3]}")
        raise RuntimeError(
            "ARCH-006 migration aborted: pre-existing rows violate the proposed "
            f"invariants: {violations}. Run `python -m scripts.audit_arch006_data` "
            "to inspect, reconcile manually (typically by attaching an address "
            "to the owning user or moving the order back to PENDING), then "
            "re-run the migration."
        )


def upgrade():
    conn = op.get_bind()
    _reconcile_legacy_violations(conn)
    _abort_if_violations_exist()

    op.create_check_constraint(
        "ck_orders_address_required_after_pending",
        "orders",
        f"status NOT IN ({_quote_in_list(ORDER_REQUIRES_ADDRESS_VALUES)}) " "OR delivery_address_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_orders_staff_creator_for_staff_source",
        "orders",
        f"order_source NOT IN ({_quote_in_list(STAFF_ORDER_SOURCES)}) " "OR created_by_staff_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_deliveries_person_required_after_assigned",
        "deliveries",
        f"status NOT IN ({_quote_in_list(DELIVERY_REQUIRES_PERSON_VALUES)}) " "OR delivery_person_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_payments_cash_completed_requires_collector",
        "payments",
        "payment_method <> 'cash' OR status <> 'completed' " "OR collected_by IS NOT NULL",
    )


def downgrade():
    op.drop_constraint("ck_payments_cash_completed_requires_collector", "payments", type_="check")
    op.drop_constraint("ck_deliveries_person_required_after_assigned", "deliveries", type_="check")
    op.drop_constraint("ck_orders_staff_creator_for_staff_source", "orders", type_="check")
    op.drop_constraint("ck_orders_address_required_after_pending", "orders", type_="check")
