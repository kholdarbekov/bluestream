"""REAL POSTGRES: a customer's cancel interleaved for real with the staff writes that race it.

F5, as amended on 2026-10-21 (docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md),
lets a customer cancel an order a driver has accepted, until the driver taps Picked up. The cancel
locks the Delivery, then the Order. The writers that can act on the same accepted stop lock before
they decide:
- the driver's own Picked up route (`PUT /staff/delivery/<id>/status`, as staff_bot sends it), whose
  only lock is the FOR UPDATE in `StaffService.update_delivery_status`;
- the admin Delivery page (`PUT /admin/deliveries/<id>`), which locks the row on entry
  (`AdminDeliveryService.update_delivery`) and again in `update_delivery_status` for Picked up;
- bulk "process", which locks the Order alone.

What each test pins:
- Customer first, admin "Returned": the page's entry lock. Without it, "Returned" never reaches
  `update_delivery_status`; it read the row unlocked and wrote `returned` over the committed cancel.
  The "Picked up" case pins only that the page re-reads the row it waited for.
- Pickup first, driver route: the FOR UPDATE in `update_delivery_status`, the only lock on that
  route. Without it the cancel does not wait, and it cancels an order the driver has just picked up.
- Pickup first, admin page: only that the admin pickup holds the row until it commits. The entry
  lock and the `update_delivery_status` lock both cover the hold point, so neither alone is pinned.
- Bulk "process": its Order lock (the item it is working on cannot be locked from outside), and
  that a refused item releases its Order lock before the next item runs.

The interleavings are forced, not hoped for: one side is held at a named point by an event, and
the other is started only then. Each thread pushes its own app context, so each has its own
session and its own Postgres connection, as two gunicorn workers would. Every wait is bounded
(30 s for an event, 60 s for a thread), so a broken lock fails the test instead of hanging it.
"""

import os
import threading
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask import current_app
from flask_jwt_extended import create_access_token
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.user import User, UserAddress
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.services.order_service import OrderService
from business_app.utils.password_security import hash_password
from shared.enums import (
    DeliveryStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserStatus,
    UserType,
)

pytestmark = pytest.mark.integration

ADMIN_DELIVERY_URL = "/api/v1/admin/deliveries/{delivery_id}"
DRIVER_STATUS_URL = "/api/v1/staff/delivery/{delivery_id}/status"
BULK_ACTIONS_URL = "/api/v1/admin/bulk-actions"
CANCEL_URL = "/api/v1/orders/{order_id}/cancel"


def _user(pg_db, phone, *, role, user_type):
    user = User(
        email=f"cancel-pickup-{uuid.uuid4().hex[:8]}@example.com",
        phone=phone,
        password_hash=hash_password("TestPassword123!"),
        first_name="Race",
        last_name=role.value.title(),
        user_type=user_type,
        role=role,
        status=UserStatus.ACTIVE,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    pg_db.session.add(user)
    pg_db.session.commit()
    return user


def _world(pg_db):
    """An unpaid Click order a driver has accepted, not yet picked up: the customer may cancel it."""
    customer = _user(pg_db, "+998901241001", role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL)
    driver = _user(pg_db, "+998901241002", role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)
    admin = _user(pg_db, "+998901241003", role=UserRole.ADMIN, user_type=UserType.STAFF)
    address = UserAddress(
        user_id=customer.id,
        title="Home",
        full_address="1 Race Street, Tashkent",
        street_address="1 Race Street",
        city="Tashkent",
        latitude=41.3111,
        longitude=69.2797,
        is_default=True,
    )
    pg_db.session.add(address)
    pg_db.session.flush()
    order = Order(
        user_id=customer.id,
        order_number=f"ORD-RACE-{uuid.uuid4().hex[:8].upper()}",
        status=OrderStatus.CONFIRMED,
        payment_method=PaymentMethod.CLICK,
        is_paid=False,
        subtotal=Decimal("15000.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("15000.00"),
        delivery_address_id=address.id,
    )
    pg_db.session.add(order)
    pg_db.session.flush()
    pg_db.session.add(
        Payment(
            order_id=order.id,
            user_id=customer.id,
            payment_method=PaymentMethod.CLICK,
            amount=Decimal("15000.00"),
            amount_collected=Decimal("0.00"),
            outstanding_amount=Decimal("15000.00"),
            currency="UZS",
            status=PaymentStatus.PENDING,
            payment_id=f"PAY-RACE-{order.id}",
        )
    )
    delivery = Delivery(
        order_id=order.id,
        status=DeliveryStatus.ASSIGNED,
        delivery_person_id=driver.id,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    pg_db.session.add(delivery)
    pg_db.session.commit()
    return SimpleNamespace(
        order_id=order.id,
        delivery_id=delivery.id,
        customer_id=customer.id,
        address_id=address.id,
        customer_headers={"Authorization": f"Bearer {create_access_token(identity=str(customer.id))}"},
        driver_headers={
            "Authorization": f"Bearer {create_access_token(identity=str(driver.id))}",
            "Content-Type": "application/json",
        },
        admin_headers={
            "Authorization": "Bearer "
            + create_access_token(identity=str(admin.id), additional_claims={"role": "admin"}),
            "Content-Type": "application/json",
        },
    )


def _lock_waiters(pg_db) -> int:
    """How many connections to this database are waiting on a lock right now. A fresh connection
    per read: `pg_stat_activity` is a snapshot taken once per transaction."""
    with pg_db.engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        ).scalar_one()


def _await_a_lock_waiter(pg_db) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _lock_waiters(pg_db):
            return True
        time.sleep(0.05)
    return False


def _thread(name, pg_app, call, answers, errors, finally_set=None):
    """`call(client)` on its own thread, app context, session and connection. Its answer lands in
    `answers[name]`; anything it raises lands in `errors`. `finally_set` is set however it ends,
    so the other side is never left waiting."""

    def run():
        with pg_app.app_context():
            try:
                response = call(pg_app.test_client())
                answers[name] = (response.status_code, response.get_json())
            except Exception as exc:  # noqa: BLE001 -- named by the assertion that reads `errors`
                errors.append(f"{name}: {exc!r}")
            finally:
                if finally_set is not None:
                    finally_set.set()

    return threading.Thread(target=run, name=name)


def _finish(*threads):
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a thread is still waiting: a lock was never released"


# What Delivery.js `handleUpdateSubmit` sends: the picked status beside the row's notes, plus the
# internal reason a move to returned asks for.
ADMIN_MOVES = {
    "picked_up": {"status": "picked_up", "notes": None},
    "returned": {"status": "returned", "notes": None, "reason": "Customer refused at the door"},
}


@pytest.mark.parametrize("move", sorted(ADMIN_MOVES))
def test_an_admin_move_arriving_while_the_customer_cancel_is_in_flight_is_refused(
    pg_app, pg_db, monkeypatch, move
):
    # `pg_app` is built without the suite's REDIS_URL, and the cancel releases stock reservations
    # in Redis: the precedent of test_sales_agent_order_hold_pg.py.
    current_app.config["REDIS_URL"] = os.environ["REDIS_URL"]
    world = _world(pg_db)
    history_before = DeliveryStatusHistory.query.filter_by(delivery_id=world.delivery_id).count()
    cancel_holds_its_locks = threading.Event()
    release_the_cancel = threading.Event()
    cascade = OrderService._cancel_delivery_for_cancelled_order

    def held_inside_the_cancel(self, order):
        if threading.current_thread().name == "customer":
            cancel_holds_its_locks.set()
            assert release_the_cancel.wait(timeout=30), "the test never released the cancel"
        return cascade(self, order)

    monkeypatch.setattr(OrderService, "_cancel_delivery_for_cancelled_order", held_inside_the_cancel)
    answers, errors = {}, []
    customer = _thread(
        "customer",
        pg_app,
        lambda client: client.post(CANCEL_URL.format(order_id=world.order_id), headers=world.customer_headers),
        answers,
        errors,
        finally_set=cancel_holds_its_locks,
    )
    admin = _thread(
        "admin",
        pg_app,
        lambda client: client.put(
            ADMIN_DELIVERY_URL.format(delivery_id=world.delivery_id),
            json=ADMIN_MOVES[move],
            headers=world.admin_headers,
        ),
        answers,
        errors,
    )

    customer.start()
    try:
        assert cancel_holds_its_locks.wait(timeout=30), "the cancel never reached its cascade"
        admin.start()
        # The admin request is queued behind the cancel's row lock before the cancel commits.
        assert _await_a_lock_waiter(pg_db), "the admin request never waited on the cancel's lock"
    finally:
        release_the_cancel.set()
        _finish(customer, *([admin] if admin.ident else []))

    assert errors == []
    (cancel_status, cancel_body), (admin_status, admin_body) = answers["customer"], answers["admin"]
    assert cancel_status == 200, cancel_body
    assert cancel_body["data"]["order"]["status"] == "cancelled"
    # The page's own answer for a cancelled row: the request re-read the row it waited for.
    assert admin_status == 400, admin_body
    assert admin_body["errors"] == [f"Cannot transition delivery from cancelled to {move}. Allowed transitions: none"]
    pg_db.session.expire_all()
    assert pg_db.session.get(Order, world.order_id).status is OrderStatus.CANCELLED
    assert pg_db.session.get(Delivery, world.delivery_id).status is DeliveryStatus.CANCELLED
    history = DeliveryStatusHistory.query.filter_by(delivery_id=world.delivery_id).all()
    assert len(history) == history_before + 1
    assert [row.new_status for row in history][-1] is DeliveryStatus.CANCELLED


# A Picked up tap, as each route's client sends it. staff_bot's `update_delivery_status` sends only
# `{"status": ...}` for picked_up: no metadata, and no address id (picked_up is not address-bound).
PICKUPS = {
    "driver-route": (DRIVER_STATUS_URL, {"status": "picked_up"}, "driver_headers"),
    "admin-delivery-page": (ADMIN_DELIVERY_URL, ADMIN_MOVES["picked_up"], "admin_headers"),
}


@pytest.mark.parametrize("route", sorted(PICKUPS))
def test_a_customer_cancel_arriving_while_a_pickup_holds_the_row_waits_and_is_refused(
    pg_app, pg_db, monkeypatch, route
):
    """The driver-route case pins the FOR UPDATE in `StaffService.update_delivery_status`: it is the
    only lock that route takes. The admin-page case pins only that the admin pickup holds the row
    until it commits (its entry lock covers the hold point too)."""
    current_app.config["REDIS_URL"] = os.environ["REDIS_URL"]
    world = _world(pg_db)
    url, body, headers = PICKUPS[route]
    pickup_holds_the_row = threading.Event()
    release_the_pickup = threading.Event()
    predicate = CashCollectionService.is_offline_settleable_electronic

    # `update_delivery_status` asks this right after its locked read of the Delivery and before
    # it writes anything, so no UPDATE holds the row yet: only a FOR UPDATE can.
    def held_after_the_locked_read(cls, order):
        if threading.current_thread().name == "pickup" and not pickup_holds_the_row.is_set():
            pickup_holds_the_row.set()
            assert release_the_pickup.wait(timeout=30), "the test never released the pickup"
        return predicate(order)

    monkeypatch.setattr(
        CashCollectionService, "is_offline_settleable_electronic", classmethod(held_after_the_locked_read)
    )
    answers, errors = {}, []
    pickup = _thread(
        "pickup",
        pg_app,
        lambda client: client.put(
            url.format(delivery_id=world.delivery_id), json=body, headers=getattr(world, headers)
        ),
        answers,
        errors,
        finally_set=pickup_holds_the_row,
    )
    customer = _thread(
        "customer",
        pg_app,
        lambda client: client.post(CANCEL_URL.format(order_id=world.order_id), headers=world.customer_headers),
        answers,
        errors,
    )

    pickup.start()
    try:
        assert pickup_holds_the_row.wait(timeout=30), "the pickup never reached its locked read"
        assert errors == [], errors
        customer.start()
        assert _await_a_lock_waiter(pg_db), "the cancel did not wait: nothing holds the Delivery row"
        assert customer.is_alive(), "the cancel finished while the pickup held the row"
    finally:
        release_the_pickup.set()
        _finish(pickup, *([customer] if customer.ident else []))

    assert errors == []
    (pickup_status, pickup_body), (cancel_status, cancel_body) = answers["pickup"], answers["customer"]
    assert pickup_status == 200, pickup_body
    # The admin page answers with the serialized delivery, the driver route with its own summary.
    answered = pickup_body["data"]["delivery"] if route == "admin-delivery-page" else pickup_body["data"]
    assert answered["status"] == "picked_up"
    assert cancel_status == 400, cancel_body
    assert cancel_body["data"] == {
        "error_code": "ORDER_NOT_CUSTOMER_CANCELLABLE",
        "reason_code": "WITH_DRIVER",
        "can_customer_cancel": False,
    }
    pg_db.session.expire_all()
    assert pg_db.session.get(Delivery, world.delivery_id).status is DeliveryStatus.PICKED_UP
    assert pg_db.session.get(Order, world.order_id).status is OrderStatus.OUT_FOR_DELIVERY


def _locked_by_someone_else(pg_db, order_id) -> bool:
    """Whether another transaction holds `order_id`'s row lock: a NOWAIT lock attempt from a fresh
    connection fails at once instead of waiting."""
    with pg_db.engine.connect() as connection:
        try:
            connection.execute(text("SELECT id FROM orders WHERE id = :id FOR UPDATE NOWAIT"), {"id": order_id})
            return False
        except OperationalError:
            return True
        finally:
            connection.rollback()


def test_bulk_process_releases_a_refused_items_order_lock_before_the_next_item(pg_app, pg_db, monkeypatch):
    """Bulk "process" locks each Order before its `confirmed` check. A refused item has written
    nothing, so it rolls back at once and releases that lock; earlier items have committed on their
    own. Held while it works on the next item, the request holds that item's Order (another
    connection's NOWAIT lock on it fails) and no longer holds the refused one (a NOWAIT lock on it
    succeeds)."""
    current_app.config["REDIS_URL"] = os.environ["REDIS_URL"]
    world = _world(pg_db)
    refused = Order(
        user_id=world.customer_id,
        order_number=f"ORD-RACE-{uuid.uuid4().hex[:8].upper()}",
        status=OrderStatus.CANCELLED,
        payment_method=PaymentMethod.CLICK,
        subtotal=Decimal("15000.00"),
        total_amount=Decimal("15000.00"),
        delivery_address_id=world.address_id,
    )
    pg_db.session.add(refused)
    pg_db.session.commit()
    refused_id = refused.id
    on_the_next_item = threading.Event()
    release_the_bulk = threading.Event()
    can_advance = OrderScheduleService.assert_can_advance

    # Asked only for an order that passed the `confirmed` check: here, the second item.
    def held_on_the_next_item(cls, order):
        if threading.current_thread().name == "bulk" and not on_the_next_item.is_set():
            on_the_next_item.set()
            assert release_the_bulk.wait(timeout=30), "the test never released the bulk request"
        return can_advance(order)

    monkeypatch.setattr(OrderScheduleService, "assert_can_advance", classmethod(held_on_the_next_item))
    answers, errors = {}, []
    bulk = _thread(
        "bulk",
        pg_app,
        # The four keys `adminService.performBulkAction` sends.
        lambda client: client.post(
            BULK_ACTIONS_URL,
            json={
                "action": "process",
                "target_type": "order",
                "target_ids": [refused_id, world.order_id],
                "parameters": {},
            },
            headers=world.admin_headers,
        ),
        answers,
        errors,
        finally_set=on_the_next_item,
    )

    bulk.start()
    try:
        assert on_the_next_item.wait(timeout=30), "the bulk request never reached its second item"
        assert errors == [], errors
        assert _locked_by_someone_else(pg_db, world.order_id), "the item being processed holds no Order lock"
        assert not _locked_by_someone_else(pg_db, refused_id), (
            "the refused item's Order lock was still held while the next item ran"
        )
    finally:
        release_the_bulk.set()
        _finish(bulk)

    assert errors == []
    status, body = answers["bulk"]
    assert status == 200, body
    assert body["data"]["results"] == {
        "success_count": 1,
        "failed_count": 1,
        "errors": [{"order_id": refused_id, "error": f"Cannot process order with status {OrderStatus.CANCELLED}"}],
        "total_errors": 1,
    }
    pg_db.session.expire_all()
    assert pg_db.session.get(Order, refused_id).status is OrderStatus.CANCELLED
    assert pg_db.session.get(Order, world.order_id).status is OrderStatus.PREPARING
