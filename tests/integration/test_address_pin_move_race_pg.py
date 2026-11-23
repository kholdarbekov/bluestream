"""REAL POSTGRES: a customer's pin move interleaved for real with the driver's "Picked up", and with a second move of
the same address (duplicate-address guard, spec 2026-11-18 §6.3; the second half is below its own header).

The move asks the customer pin rules twice: once unlocked, before its writes, and once more under the locks it then
takes on every open order to the address (`AddressPinService.apply_pin_change`, delivery first, then order). A pickup
that commits between the two must refuse the move: the second answer decides, so a pin never moves under an order the
driver is carrying.

The interleaving is forced, not hoped for: the pickup is held at its locked read of the Delivery
(`StaffService.update_delivery_status`'s FOR UPDATE), the move is started only then, and the pickup is released only
once the move waits on that lock, so the move's unlocked check has already passed. Threads, waits and the held point
follow tests/integration/test_customer_cancel_races_pg.py.
"""

import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery
from business_app.models.order import Order, OrderEditHistory
from business_app.models.user import UserAddress
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.order_address_change_service import OrderAddressChangeService
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, UserRole, UserType
from tests.integration.address_guard_geometry import BASE, at
from tests.integration.test_admin_order_address_change import address_fanout  # noqa: F401  fixture
from tests.integration.test_admin_order_reschedule import audit_events  # noqa: F401  fixture
from tests.integration.test_customer_cancel_races_pg import (
    DRIVER_STATUS_URL,
    _await_a_lock_waiter,
    _finish,
    _thread,
    _user,
)

pytestmark = pytest.mark.integration

MOVE_URL = "/api/v1/auth/addresses/{address_id}/move"


def _world(pg_db):
    """A customer's saved address at the base pin, with a cash order the driver has accepted, not yet picked up:
    the customer may still move the pin."""
    customer = _user(pg_db, "+998790000101", role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL)
    driver = _user(pg_db, "+998790000102", role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)
    address = UserAddress(
        user_id=customer.id,
        title="Uy",
        full_address="1 Race Street, Tashkent",
        street_address="1 Race Street",
        city="Tashkent",
        latitude=BASE[0],
        longitude=BASE[1],
        is_default=True,
    )
    pg_db.session.add(address)
    pg_db.session.flush()
    order = Order(
        user_id=customer.id,
        order_number=f"ORD-MOVE-{uuid.uuid4().hex[:8].upper()}",
        status=OrderStatus.CONFIRMED,
        payment_method=PaymentMethod.CASH,
        subtotal=Decimal("15000.00"),
        total_amount=Decimal("15000.00"),
        delivery_address_id=address.id,
    )
    pg_db.session.add(order)
    pg_db.session.flush()
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
        order_number=order.order_number,
        delivery_id=delivery.id,
        address_id=address.id,
        customer_headers={"Authorization": f"Bearer {create_access_token(identity=str(customer.id))}"},
        driver_headers={
            "Authorization": f"Bearer {create_access_token(identity=str(driver.id))}",
            "Content-Type": "application/json",
        },
    )


def test_a_pickup_committing_before_the_move_takes_its_locks_refuses_the_move(pg_app, pg_db, monkeypatch):
    world = _world(pg_db)
    target = at(100)
    pickup_holds_the_row = threading.Event()
    release_the_pickup = threading.Event()
    predicate = CashCollectionService.is_offline_settleable_electronic

    # `update_delivery_status` asks this right after its locked read of the Delivery and before it writes anything.
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
        # What staff_bot sends for Picked up.
        lambda client: client.put(
            DRIVER_STATUS_URL.format(delivery_id=world.delivery_id),
            json={"status": "picked_up"},
            headers=world.driver_headers,
        ),
        answers,
        errors,
        finally_set=pickup_holds_the_row,
    )
    move = _thread(
        "move",
        pg_app,
        # What the bot's Move and Edit location send.
        lambda client: client.post(
            MOVE_URL.format(address_id=world.address_id),
            json={"latitude": target[0], "longitude": target[1], "point_source": "map_pin"},
            headers=world.customer_headers,
        ),
        answers,
        errors,
    )

    pickup.start()
    try:
        assert pickup_holds_the_row.wait(timeout=30), "the pickup never reached its locked read"
        assert errors == [], errors
        move.start()
        # The move passed its unlocked check (the stop was only accepted) and now queues on the pickup's row lock.
        assert _await_a_lock_waiter(pg_db), "the move did not wait: it never reached the order locks"
        assert move.is_alive(), "the move finished while the pickup held the row"
    finally:
        release_the_pickup.set()
        _finish(pickup, *([move] if move.ident else []))

    assert errors == []
    (pickup_status, pickup_body), (move_status, move_body) = answers["pickup"], answers["move"]
    assert pickup_status == 200, pickup_body
    assert pickup_body["data"]["status"] == "picked_up"
    assert move_status == 400, move_body
    assert move_body["error_code"] == "ADDRESS_PIN_WITH_DRIVER"
    assert {k: move_body["details"][k] for k in ("reason", "order_number")} == {
        "reason": "WITH_DRIVER",
        "order_number": world.order_number,
    }
    pg_db.session.expire_all()
    kept = pg_db.session.get(UserAddress, world.address_id)
    assert (kept.latitude, kept.longitude) == BASE
    assert OrderEditHistory.query.filter_by(order_id=world.order_id).count() == 0
    assert pg_db.session.get(Delivery, world.delivery_id).status is DeliveryStatus.PICKED_UP


# --------------------------------------------------------------------------- #
# Two moves of one address: the second reads the pin the first wrote
# --------------------------------------------------------------------------- #
#
# `apply_pin_change` locks the address row too, after the order locks (delivery, order, address), and reads the pin
# again under that lock. The second of two moves therefore measures from the pin the first one wrote: its history
# row, its audit event, the driver's "moved ~N m" and the TOO_FAR cap all start from there, and an identical second
# move is a no-op. Move 1 is held inside its transaction, after every lock it takes (at its before-snapshot of the
# address); move 2 is started only then and must queue on one of those locks.


def _moving_world(pg_db, *, with_an_open_order):
    """A customer's saved address at the base pin. With `with_an_open_order`, a cash order to it that a driver with a
    Telegram chat has accepted, not yet picked up: every move redirects it and tells the driver."""
    customer = _user(pg_db, "+998790000111", role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL)
    address = UserAddress(
        user_id=customer.id,
        title="Uy",
        full_address="1 Race Street, Tashkent",
        city="Tashkent",
        latitude=BASE[0],
        longitude=BASE[1],
        is_default=True,
    )
    pg_db.session.add(address)
    pg_db.session.flush()
    order_id = None
    if with_an_open_order:
        driver = _user(pg_db, "+998790000112", role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)
        driver.telegram_id = "7000000112"
        order = Order(
            user_id=customer.id,
            order_number=f"ORD-MOVE-{uuid.uuid4().hex[:8].upper()}",
            status=OrderStatus.CONFIRMED,
            payment_method=PaymentMethod.CASH,
            subtotal=Decimal("15000.00"),
            total_amount=Decimal("15000.00"),
            delivery_address_id=address.id,
        )
        pg_db.session.add(order)
        pg_db.session.flush()
        pg_db.session.add(
            Delivery(
                order_id=order.id,
                status=DeliveryStatus.ASSIGNED,
                delivery_person_id=driver.id,
                scheduled_date=datetime.now(UTC),
                scheduled_time_slot="09:00-12:00",
            )
        )
        order_id = order.id
    pg_db.session.commit()
    return SimpleNamespace(
        address_id=address.id,
        order_id=order_id,
        customer_headers={"Authorization": f"Bearer {create_access_token(identity=str(customer.id))}"},
    )


def _second_move_queued_behind_the_first(pg_app, pg_db, monkeypatch, world, first, second):
    """POST the bot's move body to `first` and, while that move is held after its locks, to `second`; the second
    must wait on a lock the first holds. Returns {"first": (status, body), "second": (status, body)}."""
    first_holds_its_locks = threading.Event()
    release_the_first = threading.Event()
    snapshot = OrderAddressChangeService.snapshot

    def held_at_the_before_snapshot(address):
        if threading.current_thread().name == "first" and not first_holds_its_locks.is_set():
            first_holds_its_locks.set()
            assert release_the_first.wait(timeout=30), "the test never released the first move"
        return snapshot(address)

    monkeypatch.setattr(OrderAddressChangeService, "snapshot", staticmethod(held_at_the_before_snapshot))
    answers, errors = {}, []

    def move_to(point):
        return lambda client: client.post(
            MOVE_URL.format(address_id=world.address_id),
            json={"latitude": point[0], "longitude": point[1], "point_source": "map_pin"},
            headers=world.customer_headers,
        )

    first_move = _thread("first", pg_app, move_to(first), answers, errors, finally_set=first_holds_its_locks)
    second_move = _thread("second", pg_app, move_to(second), answers, errors)
    first_move.start()
    try:
        assert first_holds_its_locks.wait(timeout=30), "the first move never reached its locked read"
        assert errors == [], errors
        second_move.start()
        assert _await_a_lock_waiter(pg_db), "the second move did not wait for the first: nothing locked the address"
        assert second_move.is_alive(), "the second move finished while the first held its locks"
    finally:
        release_the_first.set()
        _finish(first_move, *([second_move] if second_move.ident else []))
    assert errors == [], errors
    return answers


def _pin_of(snapshot):
    return pytest.approx((snapshot["latitude"], snapshot["longitude"]))


def _moved_events(audit_events):  # noqa: F811
    return [event for event in audit_events if event["action"] == "address_pin_moved"]


def test_the_second_move_of_an_address_starts_from_the_pin_the_first_wrote(
    pg_app, pg_db, monkeypatch, address_fanout, audit_events  # noqa: F811
):
    world = _moving_world(pg_db, with_an_open_order=True)
    first, second = at(100), at(-40)

    answers = _second_move_queued_behind_the_first(pg_app, pg_db, monkeypatch, world, first, second)

    assert [answers[name][0] for name in ("first", "second")] == [200, 200], answers
    pg_db.session.expire_all()
    kept = pg_db.session.get(UserAddress, world.address_id)
    assert (kept.latitude, kept.longitude) == pytest.approx(second)
    history = OrderEditHistory.query.filter_by(order_id=world.order_id).order_by(OrderEditHistory.id).all()
    assert [_pin_of(row.diff["address_before"]) for row in history] == [BASE, first]
    assert [row.diff["pin_moved_m"] for row in history] == [100, 140]
    assert [call["payload"]["pin_moved_m"] for call in address_fanout.calls["driver"]] == [100, 140]
    events = _moved_events(audit_events)
    assert [_pin_of(event["old_values"]) for event in events] == [BASE, first]
    assert [event["additional_data"]["pin_moved_m"] for event in events] == [100, 140]


def test_the_second_move_of_an_address_without_orders_waits_and_starts_from_the_first_pin(
    pg_app, pg_db, monkeypatch, address_fanout, audit_events  # noqa: F811
):
    """No order lock is taken here: the address row's own lock is what queues the second move."""
    world = _moving_world(pg_db, with_an_open_order=False)
    first, second = at(100), at(-40)

    answers = _second_move_queued_behind_the_first(pg_app, pg_db, monkeypatch, world, first, second)

    assert [answers[name][0] for name in ("first", "second")] == [200, 200], answers
    pg_db.session.expire_all()
    kept = pg_db.session.get(UserAddress, world.address_id)
    assert (kept.latitude, kept.longitude) == pytest.approx(second)
    events = _moved_events(audit_events)
    assert [_pin_of(event["old_values"]) for event in events] == [BASE, first]
    assert [event["additional_data"]["pin_moved_m"] for event in events] == [100, 140]


def test_the_move_cap_is_measured_from_the_pin_the_first_move_wrote(
    pg_app, pg_db, monkeypatch, address_fanout, audit_events  # noqa: F811
):
    """100 m from the base pin passes the second move's unlocked check; 200 m from the pin move 1 wrote is past the
    cap, which the locked re-check reads."""
    cap = pg_app.config["ADDRESS_MOVE_MAX_M"]
    assert 100 <= cap - 2 and 200 >= cap + 2
    world = _moving_world(pg_db, with_an_open_order=True)
    first, second = at(100), at(-100)

    answers = _second_move_queued_behind_the_first(pg_app, pg_db, monkeypatch, world, first, second)

    assert answers["first"][0] == 200, answers
    status, body = answers["second"]
    assert (status, body["error_code"], body["details"]["reason"]) == (400, "ADDRESS_MOVE_TOO_FAR", "TOO_FAR")
    pg_db.session.expire_all()
    kept = pg_db.session.get(UserAddress, world.address_id)
    assert (kept.latitude, kept.longitude) == pytest.approx(first)
    assert OrderEditHistory.query.filter_by(order_id=world.order_id).count() == 1
    assert len(address_fanout.calls["driver"]) == 1
    assert len(_moved_events(audit_events)) == 1


def test_a_second_identical_move_is_a_no_op(pg_app, pg_db, monkeypatch, address_fanout, audit_events):  # noqa: F811
    """Two taps of the same pin: once the first lands, the second no longer moves anything, so it records, sends and
    audits nothing."""
    world = _moving_world(pg_db, with_an_open_order=True)
    target = at(100)

    answers = _second_move_queued_behind_the_first(pg_app, pg_db, monkeypatch, world, target, target)

    assert [answers[name][0] for name in ("first", "second")] == [200, 200], answers
    assert answers["second"][1]["data"]["address"]["latitude"] == pytest.approx(target[0])
    assert OrderEditHistory.query.filter_by(order_id=world.order_id).count() == 1
    assert len(address_fanout.calls["driver"]) == 1
    assert len(_moved_events(audit_events)) == 1
