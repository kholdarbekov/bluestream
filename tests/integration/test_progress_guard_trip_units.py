"""The progress guard counts 10 L like it counts 18.9 L (spec 2026-11-18 §6.5).

A stop with only 10 L on it needs a trip to be picked up or put On the way; a carried-over one late-binds to the next
trip; the capacity check covers each counted unit line against the whole crew's stops On the way; and every event
that takes a stop off the road treats the 10 L stop the way it treats 18.9 L (binding released or kept, nothing
tallied, the free figure rising once the stop leaves On the way).

Driven through the routes the staff bot and the admin pages call, with the bodies those clients send. Only Celery
publishing is faked.
"""

from datetime import date, time, timedelta

import pytest

from business_app.models.bottle import DriverBottleSessionOrder, DriverBottleSessionUnit
from business_app.models.delivery import Delivery
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import DeliveryStatus, DriverBottleSessionStatus, OrderStatus
from staff_bot.api_client import StaffAPIClient
from tests.integration.test_redispatch_reschedules_to_today import (  # noqa: F401  (fixture and helper)
    _freeze_business_clock,
    enqueued,
)
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
REDISPATCH = "/api/v1/staff/delivery/redispatch/{}"
UNASSIGN = "/api/v1/admin/dispatch/stops/{}/unassign"
ORDER_STATUS = "/api/v1/admin/orders/{}/status"
ORDER_SCHEDULE = "/api/v1/admin/orders/{}/schedule"


def _tap(client, driver, delivery_id, status, metadata=None):
    return client.put(
        STATUS.format(delivery_id),
        json=StaffAPIClient.delivery_status_body(status, metadata),
        headers=staff_headers(driver),
    )


@pytest.mark.parametrize(
    "to,on,code",
    [("picked_up", True, 400), ("in_transit", True, 400), ("picked_up", False, 200), ("in_transit", False, 200)],
)
def test_a_ten_litre_only_order_with_no_trip(app, client, db, monkeypatch, driver, small, sample_user, to, on, code):
    strict(app, monkeypatch, on)
    order = order_with_lines(db, sample_user, [(small, 4)])
    delivery = delivery_for(db, order, driver, DeliveryStatus.PICKED_UP if to == "in_transit" else DeliveryStatus.ASSIGNED)

    response = _tap(client, driver, delivery.id, to)

    assert response.status_code == code, response.get_data(as_text=True)
    if code == 400:
        assert response.get_json()["error_code"] == "BOTTLE_SESSION_REQUIRED"
        assert Delivery.query.get(delivery.id).status is (
            DeliveryStatus.PICKED_UP if to == "in_transit" else DeliveryStatus.ASSIGNED
        )
    else:
        assert Delivery.query.get(delivery.id).status.value == to


def test_a_carried_over_ten_litre_order_late_binds_to_the_next_trip(
    app, client, db, monkeypatch, enqueued, driver, small, sample_user  # noqa: F811
):
    strict(app, monkeypatch, True)
    old = trip(db, driver, units={small.id: 6}, status=DriverBottleSessionStatus.CLOSED)
    order = order_with_lines(db, sample_user, [(small, 4)])
    delivery = delivery_for(db, order, driver, DeliveryStatus.PICKED_UP)
    bind(db, old, order, driver)
    new = trip(db, driver, units={small.id: 6})

    response = _tap(client, driver, delivery.id, "in_transit")

    assert response.status_code == 200, response.get_data(as_text=True)
    db.session.expire_all()
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).one().session_id == new.id


def test_capacity_counts_a_co_drivers_ten_litre_stop_on_the_way(
    app, client, db, monkeypatch, enqueued, owner, co_driver, small, sample_user  # noqa: F811
):
    strict(app, monkeypatch, True)
    session = trip(db, owner, units={small.id: 6})
    member(db, session, co_driver)
    held = order_with_lines(db, sample_user, [(small, 4)])
    delivery_for(db, held, co_driver, DeliveryStatus.IN_TRANSIT)
    bind(db, session, held, co_driver)
    mine = order_with_lines(db, sample_user, [(small, 4)])
    mine_delivery = delivery_for(db, mine, owner, DeliveryStatus.PICKED_UP)
    bind(db, session, mine, owner)

    response = _tap(client, owner, mine_delivery.id, "in_transit")

    body = response.get_json()
    assert response.status_code == 400, body
    assert body["error_code"] == "BOTTLE_SESSION_CAPACITY_EXCEEDED"
    assert body["details"] == {
        "product_id": small.id,
        "product_name": small.name,
        "available": 2,
        "required": 4,
        "shortfall": 2,
        "validation_errors": [],
    }
    assert Delivery.query.get(mine_delivery.id).status is DeliveryStatus.PICKED_UP


def _bound_stop_on_the_way(db, driver, small, customer):
    """A trip with 12 x 10 L on board and one bound 4 x 10 L stop On the way."""
    session = trip(db, driver, units={small.id: 12})
    order = order_with_lines(db, customer, [(small, 4)], status=OrderStatus.OUT_FOR_DELIVERY)
    delivery = delivery_for(db, order, driver, DeliveryStatus.IN_TRANSIT)
    bind(db, session, order, driver)
    return session, order, delivery


def _free(session, small):
    db_session_id = session.id
    from business_app import db

    db.session.expire_all()
    return BottleTrackingService().free_units_for_session(
        db.session.get(type(session), db_session_id), small.id
    )


@pytest.mark.parametrize(
    "event,binding_kept",
    [
        ("failed", False),
        ("return_to_pool", False),
        ("reschedule", False),
        ("admin_reschedule", False),
        ("cancelled_after_pickup", True),
        ("returned", True),
    ],
)
def test_order_events_treat_ten_litre_like_full_bottles(
    app, client, db, monkeypatch, enqueued, driver, small, sample_user,  # noqa: F811
    admin_claim_headers, admin_auth_headers, operator_auth_headers, operator_user, event, binding_kept,
):
    strict(app, monkeypatch, True)
    session, order, delivery = _bound_stop_on_the_way(db, driver, small, sample_user)
    assert _free(session, small) == 8

    response = None
    if event in ("failed", "reschedule"):
        response = _tap(client, driver, delivery.id, "failed", {"fail_reason": "customer_unavailable"})
        assert response.status_code == 200, response.get_data(as_text=True)
    if event == "reschedule":
        _freeze_business_clock(monkeypatch, time(12, 0))
        response = client.post(REDISPATCH.format(delivery.id), json={}, headers=operator_auth_headers)
    elif event == "return_to_pool":
        response = client.post(UNASSIGN.format(delivery.id), json={}, headers=admin_auth_headers)
    elif event == "admin_reschedule":
        response = client.patch(
            ORDER_SCHEDULE.format(order.id),
            json={
                "delivery_date": (date.today() + timedelta(days=2)).isoformat(),
                "delivery_window_start": None,
                "delivery_window_end": None,
                "reason": "Mijoz boshqa kunni so'radi",
            },
            headers=admin_claim_headers,
        )
    elif event in ("cancelled_after_pickup", "returned"):
        target = "cancelled" if event == "cancelled_after_pickup" else "returned"
        response = client.put(
            ORDER_STATUS.format(order.id),
            json={"status": target, "notes": "", "reason": "Test reason"},
            headers=admin_claim_headers,
        )
    assert response.status_code == 200, response.get_data(as_text=True)

    db.session.expire_all()
    binding = DriverBottleSessionOrder.query.filter_by(order_id=order.id).first()
    assert (binding is not None) is binding_kept
    line = DriverBottleSessionUnit.query.filter_by(session_id=session.id, product_id=small.id).one()
    assert line.on_board == 12  # nothing delivered, so nothing tallied
    assert _free(session, small) == 12
