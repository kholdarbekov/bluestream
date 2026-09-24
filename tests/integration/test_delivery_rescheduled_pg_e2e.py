"""REAL POSTGRES: the held `rescheduled` delivery status, its driverless CHECK and its migration.

R3 and §8 of docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md. The SQLite suite
builds its schema from the models with `db.create_all()`: the status column is a plain VARCHAR
there and `ck_deliveries_no_driver_for_pool_status` does not exist. So each claim below is one
only the migrated schema can answer:

  * the `delivery_status` enum carries every Python member -- a missing label is an
    `InvalidTextRepresentation` on the first reschedule, never a SQLite failure;
  * the CHECK covers exactly `DELIVERY_DRIVERLESS_STATES` and fires, by name, for a held row that
    kept its driver;
  * the Delivery page's `?status=rescheduled` filter and its status dropdown work against the
    real enum, over HTTP, with the query the page sends;
  * migration d7e8f9a0b1c2 refuses to roll back while a delivery or a history row still says
    `rescheduled`, and rolls back cleanly once nothing does.

Harness: one MODULE-scoped migrated database, built like `place_pg_app` in
test_place_concurrency_pg_e2e.py, with a fresh app context (so a fresh session) per test.
Committed rows survive between tests, so every assertion is scoped to rows its own test made --
the HTTP tests search by a per-test order-number batch. The rollback test alone takes the
function-scoped `pg_app` from tests/integration/conftest.py: it rewrites the schema, and must not
do that to the database every other test here shares.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.utils.password_security import hash_password
from business_app.utils.state_validators import DELIVERY_DRIVERLESS_STATES
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserStatus, UserType
from tests.integration.conftest import REQUIRES_PG_REASON, _admin_engine_for, _resolve_database_url
from tests.integration.test_sales_agent_pg_constraints import (
    _constraint_def,
    _quoted_literals,
    _rollback_on_named_integrity,
)

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

API = "/api/v1"
CHECK_NAME = "ck_deliveries_no_driver_for_pool_status"
THIS_REVISION = "d7e8f9a0b1c2"
PREVIOUS_REVISION = "f2a3b4c5d6e7"
DRIVERLESS_VALUES = {status.value for status in DELIVERY_DRIVERLESS_STATES}


# =========================================================================== #
# Harness: one migrated Postgres database for the whole module
# =========================================================================== #


@pytest.fixture(scope="module")
def resched_pg_url():
    """A transient, EMPTY Postgres database; dropped on module teardown."""
    base_url = _resolve_database_url()
    if not base_url.startswith(("postgresql://", "postgresql+", "postgres://")):
        pytest.skip(REQUIRES_PG_REASON)

    from sqlalchemy.engine.url import make_url
    from sqlalchemy.exc import OperationalError

    admin_engine = _admin_engine_for(base_url)
    db_name = f"resched_e2e_{uuid.uuid4().hex[:12]}"
    quoted = f'"{db_name}"'
    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f"CREATE DATABASE {quoted}"))
    except OperationalError as exc:
        admin_engine.dispose()
        pytest.skip(f"Postgres unreachable for integration test: {exc.orig}")

    target = make_url(base_url).set(database=db_name).render_as_string(hide_password=False)
    try:
        yield target
    finally:
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :db AND pid <> pg_backend_pid()"
                ),
                {"db": db_name},
            )
            conn.execute(text(f"DROP DATABASE IF EXISTS {quoted}"))
        admin_engine.dispose()


@pytest.fixture(scope="module")
def resched_pg_app(resched_pg_url):
    """A Flask app on that database with the REAL migration chain applied."""
    from flask_migrate import upgrade

    from business_app import create_app

    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": resched_pg_url,
            "SQLALCHEMY_TRACK_MODIFICATIONS": False,
            "SECRET_KEY": "test-secret-key-for-reschedule-pg-e2e-32c",
            "JWT_SECRET_KEY": "test-jwt-secret-key-for-reschedule-pg-e2e",
            "CELERY_ALWAYS_EAGER": True,
            "WTF_CSRF_ENABLED": False,
        }
    )
    with app.app_context():
        upgrade(revision="head")
    yield app


@pytest.fixture
def rpapp(resched_pg_app):
    """A FRESH app context (hence a fresh session) per test."""
    from business_app import db as _db

    with resched_pg_app.app_context():
        try:
            yield resched_pg_app
        finally:
            _db.session.rollback()
            _db.session.remove()


@pytest.fixture
def rpdb(rpapp):
    from business_app import db as _db

    return _db


@pytest.fixture
def rpclient(rpapp):
    return rpapp.test_client()


# =========================================================================== #
# Builders
# =========================================================================== #

_SEQ = [0]


def _uniq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _batch() -> str:
    """A per-test order-number prefix: the Delivery page's search box scopes a list to it."""
    return f"ORD-RSCH-{uuid.uuid4().hex[:8].upper()}"


def _user(db, *, role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL) -> User:
    n = _uniq()
    user = User(
        email=f"resched.pg.{n}.{uuid.uuid4().hex[:6]}@example.com",
        phone=f"+9989{710000000 + n}",
        password_hash=hash_password("TestPassword123!"),
        first_name="Resched",
        last_name=f"User{n}",
        user_type=user_type,
        role=role,
        status=UserStatus.ACTIVE,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.commit()
    return user


def _confirmed_order(db, *, order_number: str) -> Order:
    """A CONFIRMED order with its own customer and address, committed.

    CONFIRMED needs the address: `ck_orders_address_required_after_pending`.
    """
    customer = _user(db)
    address = UserAddress(
        user_id=customer.id,
        title="Home",
        full_address=f"{order_number} Test Street, Tashkent",
        street_address=f"{order_number} Test Street",
        city="Tashkent",
        latitude=41.3111,
        longitude=69.2797,
        is_default=True,
    )
    db.session.add(address)
    db.session.flush()
    order = Order(
        user_id=customer.id,
        order_number=order_number,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("18000.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("18000.00"),
        delivery_address_id=address.id,
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.commit()
    return order


def _delivery(order: Order, *, status: DeliveryStatus, driver_id=None) -> Delivery:
    """An UNSAVED delivery for `order`: the constraint tests must attempt the insert themselves."""
    return Delivery(
        order_id=order.id,
        delivery_person_id=driver_id,
        status=status,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )


def _add(db, row):
    db.session.add(row)
    db.session.commit()
    return row


def _admin_headers(user: User) -> dict:
    """A real JWT with the role CLAIM, on a real ADMIN row (`validate_admin_action` reads the DB
    role and status too)."""
    from flask_jwt_extended import create_access_token

    token = create_access_token(identity=str(user.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _version(db) -> str:
    return db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


# =========================================================================== #
# The migrated schema
# =========================================================================== #


def test_the_delivery_status_enum_carries_every_python_member(rpdb):
    labels = set(
        rpdb.session.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid "
                "WHERE t.typname = 'delivery_status'"
            )
        ).scalars()
    )

    assert labels == {status.value for status in DeliveryStatus}


def test_a_held_row_and_its_history_entry_persist_as_rescheduled(rpdb):
    order = _confirmed_order(rpdb, order_number=_batch())
    held_id = _add(rpdb, _delivery(order, status=DeliveryStatus.RESCHEDULED)).id
    _add(
        rpdb,
        DeliveryStatusHistory(
            delivery_id=held_id,
            old_status=DeliveryStatus.ASSIGNED,
            new_status=DeliveryStatus.RESCHEDULED,
            reason="Customer away until Friday",
            notes="Rescheduled to 2026-09-26",
        ),
    )
    rpdb.session.expire_all()

    stored = rpdb.session.execute(
        text("SELECT status::text, delivery_person_id FROM deliveries WHERE id = :id"), {"id": held_id}
    ).one()
    assert tuple(stored) == ("rescheduled", None)
    assert rpdb.session.get(Delivery, held_id).status is DeliveryStatus.RESCHEDULED
    history = DeliveryStatusHistory.query.filter_by(delivery_id=held_id).one()
    assert (history.old_status, history.new_status) == (DeliveryStatus.ASSIGNED, DeliveryStatus.RESCHEDULED)


def test_the_driverless_check_covers_exactly_the_driverless_states(rpdb):
    """The migration hand-writes its value list (a migration must not import live code). This is
    what keeps that list equal to `DELIVERY_DRIVERLESS_STATES`."""
    definition = _constraint_def(rpdb, "deliveries", CHECK_NAME)

    assert definition is not None, f"{CHECK_NAME} is missing from the migrated schema"
    assert _quoted_literals(definition) == DRIVERLESS_VALUES


@pytest.mark.parametrize(
    "status", sorted(DELIVERY_DRIVERLESS_STATES, key=lambda s: s.value), ids=lambda s: s.value
)
def test_a_driverless_status_that_kept_its_driver_is_refused_by_name(rpdb, status):
    driver = _user(rpdb, role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)
    order = _confirmed_order(rpdb, order_number=_batch())
    order_id = order.id

    _rollback_on_named_integrity(rpdb, _delivery(order, status=status, driver_id=driver.id), CHECK_NAME)

    assert Delivery.query.filter_by(order_id=order_id).count() == 0


# =========================================================================== #
# The Delivery page, over HTTP
# =========================================================================== #


def test_the_delivery_page_status_filter_lists_only_the_held_row(rpdb, rpclient):
    """`?status=rescheduled` answered 400 "Invalid delivery status" while STATUS_ALIASES was a
    hand-written list. The query string is the one Delivery.js sends."""
    headers = _admin_headers(_user(rpdb, role=UserRole.ADMIN, user_type=UserType.STAFF))
    batch = _batch()
    held_id = _add(
        rpdb, _delivery(_confirmed_order(rpdb, order_number=f"{batch}-1"), status=DeliveryStatus.RESCHEDULED)
    ).id
    pooled_id = _add(
        rpdb, _delivery(_confirmed_order(rpdb, order_number=f"{batch}-2"), status=DeliveryStatus.SCHEDULED)
    ).id

    filtered = rpclient.get(
        f"{API}/admin/deliveries",
        query_string={"page": 1, "per_page": 20, "search": batch, "status": "rescheduled"},
        headers=headers,
    )

    assert filtered.status_code == 200, filtered.get_data(as_text=True)
    body = filtered.get_json()
    assert [(row["id"], row["status"], row["driver_id"]) for row in body["data"]["items"]] == [
        (held_id, "rescheduled", None)
    ]
    assert body["meta"]["summary"]["rescheduled_deliveries"] == 1
    # Held for its day, not waiting for a driver: it is not "unassigned".
    assert body["meta"]["summary"]["unassigned_deliveries"] == 0

    unfiltered = rpclient.get(
        f"{API}/admin/deliveries",
        query_string={"page": 1, "per_page": 20, "search": batch},
        headers=headers,
    )

    assert unfiltered.status_code == 200, unfiltered.get_data(as_text=True)
    everything = unfiltered.get_json()
    assert sorted(row["id"] for row in everything["data"]["items"]) == sorted([held_id, pooled_id])
    summary = everything["meta"]["summary"]
    assert summary["status_breakdown"] == {"rescheduled": 1, "scheduled": 1}
    assert (summary["rescheduled_deliveries"], summary["scheduled_deliveries"]) == (1, 1)
    assert summary["unassigned_deliveries"] == 1


def test_the_delivery_page_can_neither_move_a_held_row_nor_make_one(rpdb, rpclient):
    """ADMIN_ALLOWED_TRANSITIONS: a held row has no admin move (R22: a reschedule to today is the
    only early release), and no admin move leads into it (only OrderScheduleService puts a row
    there)."""
    headers = _admin_headers(_user(rpdb, role=UserRole.ADMIN, user_type=UserType.STAFF))
    held_id = _add(
        rpdb, _delivery(_confirmed_order(rpdb, order_number=_batch()), status=DeliveryStatus.RESCHEDULED)
    ).id
    pooled_id = _add(
        rpdb, _delivery(_confirmed_order(rpdb, order_number=_batch()), status=DeliveryStatus.SCHEDULED)
    ).id

    release = rpclient.put(f"{API}/admin/deliveries/{held_id}", json={"status": "scheduled"}, headers=headers)
    hold = rpclient.put(f"{API}/admin/deliveries/{pooled_id}", json={"status": "rescheduled"}, headers=headers)

    assert release.status_code == 400, release.get_data(as_text=True)
    assert release.get_json()["errors"] == [
        "Cannot transition delivery from rescheduled to scheduled. Allowed transitions: none"
    ]
    assert hold.status_code == 400, hold.get_data(as_text=True)
    assert hold.get_json()["errors"] == [
        "Cannot transition delivery from scheduled to rescheduled. Allowed transitions: pending, returned"
    ]
    rpdb.session.expire_all()
    assert rpdb.session.get(Delivery, held_id).status is DeliveryStatus.RESCHEDULED
    assert rpdb.session.get(Delivery, pooled_id).status is DeliveryStatus.SCHEDULED
    touched = DeliveryStatusHistory.query.filter(DeliveryStatusHistory.delivery_id.in_([held_id, pooled_id]))
    assert touched.count() == 0


# =========================================================================== #
# The migration's rollback guard (its own database: it rewrites the schema)
# =========================================================================== #


def test_the_migration_refuses_to_roll_back_while_anything_says_rescheduled(pg_app, pg_db):
    """§8: the previous code can load neither a held delivery nor a history row naming the
    status, so the downgrade refuses on either one. Once neither is left it succeeds and restores
    the scheduled/pending CHECK. Postgres cannot drop the enum label, so the label stays, which is
    harmless."""
    from alembic import command

    config = pg_app.extensions["migrate"].migrate.get_config()
    head = _version(pg_db)
    held_id = _add(
        pg_db, _delivery(_confirmed_order(pg_db, order_number=_batch()), status=DeliveryStatus.RESCHEDULED)
    ).id
    _add(
        pg_db,
        DeliveryStatusHistory(
            delivery_id=held_id, old_status=DeliveryStatus.SCHEDULED, new_status=DeliveryStatus.RESCHEDULED
        ),
    )

    def release_session():
        # Alembic migrates on its own connection, and dropping the CHECK takes an exclusive lock
        # on `deliveries`: this session must not be holding a transaction open when it does.
        pg_db.session.remove()

    release_session()
    with pytest.raises(
        RuntimeError, match=rf"Downgrade of {THIS_REVISION} refused: 1 deliveries and 1 delivery_status_history rows"
    ):
        command.downgrade(config, PREVIOUS_REVISION)
    assert _version(pg_db) == head

    # The live row is gone from the status, but its history still names it.
    pg_db.session.execute(text("UPDATE deliveries SET status = 'scheduled' WHERE id = :id"), {"id": held_id})
    pg_db.session.commit()
    release_session()
    with pytest.raises(RuntimeError, match=r"refused: 0 deliveries and 1 delivery_status_history rows"):
        command.downgrade(config, PREVIOUS_REVISION)
    assert _version(pg_db) == head

    pg_db.session.execute(text("DELETE FROM delivery_status_history WHERE delivery_id = :id"), {"id": held_id})
    pg_db.session.commit()
    release_session()
    command.downgrade(config, PREVIOUS_REVISION)

    assert _version(pg_db) == PREVIOUS_REVISION
    assert _quoted_literals(_constraint_def(pg_db, "deliveries", CHECK_NAME)) == {"scheduled", "pending"}

    # And forward again: ADD VALUE IF NOT EXISTS is a no-op the second time round.
    release_session()
    command.upgrade(config, "head")

    assert _version(pg_db) == head
    assert _quoted_literals(_constraint_def(pg_db, "deliveries", CHECK_NAME)) == DRIVERLESS_VALUES
