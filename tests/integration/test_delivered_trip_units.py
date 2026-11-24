"""A Delivered adds the order's 10 L to its trip's line, and only to an open trip (spec 2026-11-18 §6.5, call 19).

The customer bottle ledger stays returnable-only: a 10 L-only order writes no ledger or balance row and moves no
empties. The trip part runs for anything the order puts on the trip, in one tally with the 18.9 L bottles. A binding
on a trip that is no longer open is treated as no binding: strict refuses the Delivered, legacy logs it, and the closed
trip's sealed figures never change (18.9 L too).

Driven through the routes the staff bot and the admin Orders page call, with the bodies those clients send. Only
Celery publishing is faked.
"""

import logging

import pytest

from business_app.models.bottle import (
    BottleBalance,
    BottleLedger,
    DriverBottleSession,
    DriverBottleSessionOrder,
    DriverBottleSessionUnit,
)
from business_app.models.order import Order
from shared.enums import DeliveryStatus, DriverBottleSessionStatus, OrderStatus
from staff_bot.api_client import StaffAPIClient
from tests.integration.reorder_reminder_builders import captured_logs
from tests.integration.test_redispatch_reschedules_to_today import enqueued  # noqa: F401  (fixture)
from tests.integration.trip_unit_builders import (  # noqa: F401  (fixtures)
    big,
    bind,
    co_driver,
    delivery_for,
    driver,
    member,
    order_with_lines,
    owner,
    small,
    staff_headers,
    strict,
    trip,
)

pytestmark = pytest.mark.integration

STATUS = "/api/v1/staff/delivery/{}/status"
ACTIVE = "/api/v1/staff/delivery/active"
ORDER_STATUS = "/api/v1/admin/orders/{}/status"
ORDER_EDIT = "/api/v1/admin/orders/{}/edit"


def _delivered(client, by, delivery_id, metadata=None):
    """The staff bot's Delivered tap by driver `by`, with the body `StaffAPIClient` sends."""
    return client.put(
        STATUS.format(delivery_id),
        json=StaffAPIClient.delivery_status_body("delivered", metadata),
        headers=staff_headers(by),
    )


def _stop(db, customer, lines, carrier, session):
    """An order of `lines`, out for delivery, its driver `carrier` at the door, bound to `session`."""
    order = order_with_lines(db, customer, lines, status=OrderStatus.OUT_FOR_DELIVERY)
    delivery = delivery_for(db, order, carrier, DeliveryStatus.ARRIVED)
    bind(db, session, order, carrier)
    return order, delivery


def _fresh(db, session_id):
    """The trip as committed. The test shares the requests' app context, so their teardown (`db.session.remove()`)
    never runs: roll back here what a refused request flushed and left uncommitted."""
    db.session.rollback()
    return db.session.get(DriverBottleSession, session_id)


@pytest.mark.parametrize("metadata", [None, {"bottles_returned": 2}])
def test_a_ten_litre_only_delivered_adds_to_its_line_and_nothing_to_the_customer_ledger(
    client, db, enqueued, driver, small, sample_user, metadata  # noqa: F811
):
    s = trip(db, driver, units={small.id: 12})
    order, d = _stop(db, sample_user, [(small, 4)], driver, s)
    active = client.get(ACTIVE, headers=staff_headers(driver)).get_json()["data"]["items"]  # the route the bot reads
    assert [x["expected_returnable_bottles"] for x in active] == [0.0]  # the bot asks for no empties

    r = _delivered(client, driver, d.id, metadata)

    assert r.status_code == 200, r.get_data(as_text=True)
    s = _fresh(db, s.id)
    assert (s.unit_lines[0].delivered, s.bottles_delivered, s.bottles_collected_from_customers) == (4, 0, 0)
    assert BottleLedger.query.filter_by(order_id=order.id).count() == 0 and BottleBalance.query.count() == 0


def test_a_mixed_order_adds_to_both(client, db, enqueued, driver, big, small, sample_user):  # noqa: F811
    s = trip(db, driver, bottles=10, units={small.id: 12})
    order, d = _stop(db, sample_user, [(big, 3), (small, 2)], driver, s)

    r = _delivered(client, driver, d.id, {"bottles_returned": 2})

    assert r.status_code == 200, r.get_data(as_text=True)
    s = _fresh(db, s.id)
    assert (s.bottles_delivered, s.bottles_collected_from_customers, s.unit_lines[0].delivered) == (3, 2, 2)
    ledger = sorted(
        (row.event_type.value, int(row.quantity)) for row in BottleLedger.query.filter_by(order_id=order.id)
    )
    assert ledger == [("delivery", 3), ("return_on_delivery", -2)]


def test_a_co_drivers_delivery_lands_on_the_owners_line(
    client, db, enqueued, owner, co_driver, small, sample_user  # noqa: F811
):
    s = trip(db, owner, units={small.id: 12})
    member(db, s, co_driver)
    _order, d = _stop(db, sample_user, [(small, 4)], co_driver, s)

    r = _delivered(client, co_driver, d.id)

    assert r.status_code == 200, r.get_data(as_text=True)
    s = _fresh(db, s.id)
    assert [(line.product_id, line.delivered) for line in s.unit_lines] == [(small.id, 4)]
    assert DriverBottleSession.query.count() == 1  # nothing opened for the co-driver


def test_a_trip_without_a_ten_litre_line_tallies_no_ten_litre(
    client, db, enqueued, caplog, driver, big, small, sample_user  # noqa: F811
):
    s = trip(db, driver, bottles=10)
    _order, d = _stop(db, sample_user, [(big, 3), (small, 2)], driver, s)

    with captured_logs(caplog, logging.getLogger("business_app.services.bottle_tracking_service"), logging.WARNING):
        r = _delivered(client, driver, d.id)

    assert r.status_code == 200, r.get_data(as_text=True)
    s = _fresh(db, s.id)
    assert s.bottles_delivered == 3
    assert DriverBottleSessionUnit.query.count() == 0
    assert f"[BOTTLE] trip={s.id} has no line for product={small.id}; not tallied" in caplog.messages


@pytest.mark.parametrize("on,code", [(True, 400), (False, 200)])
def test_a_binding_on_a_closed_trip_tallies_nothing_to_it(
    app, client, db, monkeypatch, enqueued, admin_claim_headers, driver, big, small, sample_user, on, code  # noqa: F811
):
    """The driver handed the order back at the door (Returned on the Orders page), closed his trip, and has no trip
    now. The order's binding stays on the closed trip, as a finished order's does. The office puts it back to pending,
    confirms it and marks it Delivered: the closed trip is not the trip that carried it this time."""
    strict(app, monkeypatch, on)
    s = trip(db, driver, bottles=10, units={small.id: 12}, status=DriverBottleSessionStatus.CLOSED)
    order = order_with_lines(db, sample_user, [(big, 3), (small, 2)], status=OrderStatus.RETURNED)
    delivery_for(db, order, driver, DeliveryStatus.ARRIVED)
    bind(db, s, order, driver)
    for status in ("pending", "confirmed"):
        moved = client.put(
            ORDER_STATUS.format(order.id), json={"status": status, "notes": ""}, headers=admin_claim_headers
        )
        assert moved.status_code == 200, moved.get_data(as_text=True)

    r = client.put(
        ORDER_STATUS.format(order.id), json={"status": "delivered", "notes": ""}, headers=admin_claim_headers
    )

    assert r.status_code == code, r.get_data(as_text=True)
    if code == 400:
        assert r.get_json()["data"]["error_code"] == "BOTTLE_SESSION_REQUIRED"
    s = _fresh(db, s.id)
    assert (s.status, s.bottles_delivered, s.unit_lines[0].delivered) == (DriverBottleSessionStatus.CLOSED, 0, 0)
    assert db.session.get(Order, order.id).status is (OrderStatus.CONFIRMED if on else OrderStatus.DELIVERED)


def test_a_legacy_delivered_that_tallied_nothing_leaves_the_order_on_no_trip_so_a_later_edit_reopens_nothing(
    app, client, db, monkeypatch, enqueued, admin_claim_headers, driver, big, small, sample_user  # noqa: F811
):
    """Legacy mode (the config default). The order came back Returned on the driver's trip, he closed the trip, and the
    office then put the order back to pending, confirmed it and marked it Delivered while he had no trip: nothing was
    tallied anywhere. The order is then bound to no trip, so the binding only ever names the trip that counted the
    order: when the office corrects it the same day (4 x 10 L to 2), the closed trip, which never counted this
    delivery, is not reopened or changed (it was reopened, 2 x 10 L were taken off a delivery it never saw, and the
    reopened trip then stood in the way of the driver's next Load)."""
    strict(app, monkeypatch, False)
    s = trip(db, driver, bottles=10, units={small.id: 12}, status=DriverBottleSessionStatus.CLOSED)
    order = order_with_lines(db, sample_user, [(big, 3), (small, 4)], status=OrderStatus.RETURNED)
    delivery_for(db, order, driver, DeliveryStatus.ARRIVED)
    bind(db, s, order, driver)
    for status in ("pending", "confirmed", "delivered"):
        moved = client.put(
            ORDER_STATUS.format(order.id), json={"status": status, "notes": ""}, headers=admin_claim_headers
        )
        assert moved.status_code == 200, moved.get_data(as_text=True)
    body = {  # what Orders.js sends for Apply: every line, the 10 L one at its new quantity
        "items": [
            {
                "orderItemId": item.id,
                "productId": item.product_id,
                "quantity": 2 if item.product_id == small.id else item.quantity,
            }
            for item in db.session.get(Order, order.id).order_items
        ],
        "reason": "Customer kept only two",
    }

    r = client.post(ORDER_EDIT.format(order.id), json=body, headers=admin_claim_headers)

    assert r.status_code == 200, r.get_data(as_text=True)
    summary = r.get_json()["data"]["cascade_summary"]
    assert summary["session_reopened"] == {}
    assert summary["trip_effect"] == {
        "session_id": None,
        "bottles": 0.0,
        "units": [],
        "reopens": False,
        "reopen_refusal": None,
    }
    s = _fresh(db, s.id)
    assert (s.status, s.reopen_count, s.bottles_delivered) == (DriverBottleSessionStatus.CLOSED, 0, 0)
    assert [(line.product_id, line.delivered) for line in s.unit_lines] == [(small.id, 0)]
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).count() == 0
