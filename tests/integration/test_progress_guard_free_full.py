"""The progress guard counts FREE FULL bottles at the two steps where a stop can promise water (spec A6, A8, A9).

1. Leaving ASSIGNED (Picked up, or straight to On the way): a bottle trip is required and its free full bottles must
   cover the order, whether or not the order is already bound to the trip.
2. Entering On the way: the crew's free full bottles, not counting this stop, must cover it. This is where bottles
   are reserved, so the customer is never told "on the way" for water that is not on the truck.
Arrived and Delivered are not counted again: the stop is already promised. Admin status moves (the Deliveries page
and the bulk "mark in transit") skip the count, but still need the driver to be on a trip.

Driven through the HTTP routes the staff bot, the driver app and the admin pages call, with real JWTs and the bodies
those clients send. Only Celery publishing is faked (`enqueued` binds each publish against the task's signature).
"""

from decimal import Decimal

import pytest

from business_app.models.bottle import DriverBottleSessionOrder
from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.services.bottle_tracking_service import BottleTrackingService, TripUnits
from business_app.tasks.notification_tasks import send_delivery_update_task
from shared.enums import DeliveryStatus
from staff_bot.api_client import StaffAPIClient
from tests.integration.test_assign_free_full_api import _admin_headers, _pool_delivery, _session
from tests.integration.test_redispatch_reschedules_to_today import enqueued  # noqa: F401 -- a fixture
from tests.unit.test_staff_error_endpoints import _assigned_delivery, _driver, _headers, _order_with_bottles

pytestmark = pytest.mark.integration

ACCEPT = "/api/v1/staff/delivery/accept/{}"
STATUS = "/api/v1/staff/delivery/{}/status"
START_DELIVERY = "/api/v1/delivery/driver/start-delivery/{}"
ADMIN_DELIVERY = "/api/v1/admin/deliveries/{}"
BULK_ACTIONS = "/api/v1/admin/bulk-actions"


@pytest.fixture
def strict(app, monkeypatch):
    """Production runs strict; the code default is lenient."""
    monkeypatch.setitem(app.config, "BOTTLE_SESSION_ENFORCEMENT_STRICT", True)


def _tap(client, app, driver, delivery_id, status):
    """The staff bot's status tap, with the body `StaffAPIClient.update_delivery_status` sends."""
    return client.put(
        STATUS.format(delivery_id), json=StaffAPIClient.delivery_status_body(status), headers=_headers(app, driver.id)
    )


def _admin_moves(client, app, admin_user, delivery_id, status):
    """What Delivery.js `handleUpdateSubmit` sends for a status pick: the status beside the row's notes."""
    return client.put(
        ADMIN_DELIVERY.format(delivery_id),
        json={"status": status, "notes": None},
        headers=_admin_headers(app, admin_user),
    )


def _bulk_mark_in_transit(client, app, admin_user, delivery_ids):
    return client.post(
        BULK_ACTIONS,
        json={"action": "mark_in_transit", "target_type": "delivery", "target_ids": delivery_ids},
        headers=_admin_headers(app, admin_user),
    )


def _bound(db, session, order, driver):
    """The order on the driver's trip, as the bot accept leaves it."""
    db.session.add(DriverBottleSessionOrder(session_id=session.id, order_id=order.id, accepted_by_driver_id=driver.id))
    db.session.commit()


def _capacity_refusal(available, required):
    """The wire `details` of BOTTLE_SESSION_CAPACITY_EXCEEDED. `@handle_api_exception` folds the
    `validation_errors: []` every ValidationError carries into it."""
    return {
        "available": available,
        "required": required,
        "shortfall": required - available,
        "validation_errors": [],
    }


def _status_of(delivery_id):
    return Delivery.query.get(delivery_id).status


def _customer_updates(published):
    """Every customer update a tap published, as (delivery id, status the history row moved to)."""
    rows = []
    for name, args, kwargs in published:
        if name != send_delivery_update_task.name:
            continue
        assert kwargs == {}, kwargs
        history = DeliveryStatusHistory.query.get(args[0])
        rows.append((history.delivery_id, history.new_status.value))
    return rows


# --------------------------------------------------------------------------- #
# The driver's own taps
# --------------------------------------------------------------------------- #


def test_picked_up_refused_when_only_empties(
    app, client, db, strict, enqueued, sample_user, sample_product  # noqa: F811 -- `enqueued` is the fixture
):
    """18 bottles on the truck, 3 of them full. The order is already on the trip, which used to skip the count."""
    driver = _driver(db, "+998901005001")
    session = _session(db, driver, loaded=3, collected=15)
    order = _order_with_bottles(db, sample_user, sample_product, quantity=8)
    delivery = _assigned_delivery(db, order, driver)
    _bound(db, session, order, driver)

    response = _tap(client, app, driver, delivery.id, "picked_up")

    body = response.get_json()
    assert response.status_code == 400, body
    assert body["error_code"] == "BOTTLE_SESSION_CAPACITY_EXCEEDED"
    assert body["details"] == _capacity_refusal(available=3, required=8)
    db.session.expire_all()
    assert _status_of(delivery.id) is DeliveryStatus.ASSIGNED
    assert DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id).count() == 0
    assert _customer_updates(enqueued) == []


def test_second_on_the_way_refused(
    app, client, db, strict, enqueued, sample_user, sample_product  # noqa: F811 -- `enqueued` is the fixture
):
    """Spec §5 A8: 10 full bottles, two 8-bottle orders. Both pass Picked up, which reserves nothing. The first
    On the way reserves 8; the second is refused with 2 free / 8 needed, and its customer is told nothing."""
    driver = _driver(db, "+998901005002")
    session = _session(db, driver, loaded=10)
    d1 = _pool_delivery(db, _order_with_bottles(db, sample_user, sample_product, quantity=8))
    d2 = _pool_delivery(db, _order_with_bottles(db, sample_user, sample_product, quantity=8))
    for delivery in (d1, d2):
        accepted = client.post(ACCEPT.format(delivery.id), headers=_headers(app, driver.id))
        assert accepted.status_code == 200, accepted.get_json()
    assert {row.order_id for row in DriverBottleSessionOrder.query.filter_by(session_id=session.id)} == {
        d1.order_id,
        d2.order_id,
    }

    for delivery in (d1, d2):
        picked = _tap(client, app, driver, delivery.id, "picked_up")
        assert picked.status_code == 200, picked.get_json()
    first = _tap(client, app, driver, d1.id, "in_transit")
    second = _tap(client, app, driver, d2.id, "in_transit")

    assert first.status_code == 200, first.get_json()
    body = second.get_json()
    assert second.status_code == 400, body
    assert body["error_code"] == "BOTTLE_SESSION_CAPACITY_EXCEEDED"
    assert body["details"] == _capacity_refusal(available=2, required=8)
    db.session.expire_all()
    assert (_status_of(d1.id), _status_of(d2.id)) == (DeliveryStatus.IN_TRANSIT, DeliveryStatus.PICKED_UP)
    assert _customer_updates(enqueued) == [
        (d1.id, "assigned"),
        (d2.id, "assigned"),
        (d1.id, "picked_up"),
        (d2.id, "picked_up"),
        (d1.id, "in_transit"),
    ]


def test_arrived_and_delivered_not_rechecked(app, client, db, strict, admin_user, sample_user, sample_product):
    """Loaded 8, d1 needs 8. Once d1 is on the way, an admin sends d2 (8 more) on its way as well, so the truck
    has promised 16 of 8. Re-counting d1 at Arrived or Delivered would now refuse it at the door."""
    driver = _driver(db, "+998901005003")
    session = _session(db, driver, loaded=8)
    order1 = _order_with_bottles(db, sample_user, sample_product, quantity=8)
    d1 = _assigned_delivery(db, order1, driver)
    _bound(db, session, order1, driver)
    d2 = _assigned_delivery(db, _order_with_bottles(db, sample_user, sample_product, quantity=8), driver)

    for status in ("picked_up", "in_transit"):
        response = _tap(client, app, driver, d1.id, status)
        assert response.status_code == 200, (status, response.get_json())
    bulk = _bulk_mark_in_transit(client, app, admin_user, [d2.id])
    assert bulk.status_code == 200, bulk.get_json()
    assert bulk.get_json()["data"]["results"]["success_count"] == 1, bulk.get_json()
    db.session.expire_all()
    assert BottleTrackingService().free_full_bottles_for_session(session, exclude_delivery_id=d1.id) == 0

    for status in ("arrived", "delivered"):
        response = _tap(client, app, driver, d1.id, status)
        assert response.status_code == 200, (status, response.get_json())
    db.session.expire_all()
    assert _status_of(d1.id) is DeliveryStatus.DELIVERED


@pytest.mark.parametrize("loaded, expected_status", [(8, 200), (7, 400)])
def test_assigned_straight_to_in_transit_checks_once(
    app, client, db, strict, monkeypatch, sample_user, sample_product, loaded, expected_status
):
    """The driver app's Start (`begin_delivery_in_transit`) skips Picked up. Leaving ASSIGNED and entering On the
    way are the same check, run once, and the stop never counts against itself."""
    driver = _driver(db, "+998901005004")
    session = _session(db, driver, loaded=loaded)
    order = _order_with_bottles(db, sample_user, sample_product, quantity=8)
    delivery = _assigned_delivery(db, order, driver)
    _bound(db, session, order, driver)
    checks = []
    real_check = BottleTrackingService.assert_order_within_session_capacity

    def recorded(self, checked_session, trip_units, *, exclude_delivery_id=None):
        checks.append((checked_session.id, trip_units, exclude_delivery_id))
        return real_check(self, checked_session, trip_units, exclude_delivery_id=exclude_delivery_id)

    monkeypatch.setattr(BottleTrackingService, "assert_order_within_session_capacity", recorded)

    response = client.post(START_DELIVERY.format(delivery.id), headers=_headers(app, driver.id))

    body = response.get_json()
    assert response.status_code == expected_status, body
    assert checks == [(session.id, TripUnits(Decimal("8.00"), {}), delivery.id)]
    db.session.expire_all()
    if expected_status == 200:
        assert _status_of(delivery.id) is DeliveryStatus.IN_TRANSIT
    else:
        assert body["error_code"] == "BOTTLE_SESSION_CAPACITY_EXCEEDED"
        assert _status_of(delivery.id) is DeliveryStatus.ASSIGNED


# --------------------------------------------------------------------------- #
# Admin status moves (A9)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "from_status, to_status",
    [(DeliveryStatus.ASSIGNED, "picked_up"), (DeliveryStatus.PICKED_UP, "in_transit")],
)
def test_admin_status_move_skips_count(
    app, client, db, strict, admin_user, sample_user, sample_product, from_status, to_status
):
    """3 full and 15 empties for an 8-bottle order: the Deliveries page still moves it. The order is booked to the
    driver's trip, so the bottles it takes are counted against that truck."""
    driver = _driver(db, "+998901005005")
    session = _session(db, driver, loaded=3, collected=15)
    order = _order_with_bottles(db, sample_user, sample_product, quantity=8)
    delivery = _assigned_delivery(db, order, driver, status=from_status)

    response = _admin_moves(client, app, admin_user, delivery.id, to_status)

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["delivery"]["status"] == to_status
    db.session.expire_all()
    assert _status_of(delivery.id) is DeliveryStatus(to_status)
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).one().session_id == session.id


def test_admin_status_move_still_needs_trip(app, client, db, strict, admin_user, sample_user, sample_product):
    """No trip, no move: the bottles must be booked to some truck. The page maps the code to its own words."""
    driver = _driver(db, "+998901005006")
    delivery = _assigned_delivery(db, _order_with_bottles(db, sample_user, sample_product, quantity=8), driver)

    response = _admin_moves(client, app, admin_user, delivery.id, "picked_up")

    body = response.get_json()
    assert response.status_code == 400, body
    assert body["data"] == {"error_code": "BOTTLE_SESSION_REQUIRED"}
    db.session.expire_all()
    assert _status_of(delivery.id) is DeliveryStatus.ASSIGNED


def test_bulk_mark_in_transit_skips_count(app, client, db, strict, admin_user, sample_user, sample_product):
    """The bulk "mark in transit" of a short driver's stop goes through; a driver with no trip is still refused."""
    short = _driver(db, "+998901005007")
    session = _session(db, short, loaded=3, collected=15)
    order = _order_with_bottles(db, sample_user, sample_product, quantity=8)
    covered_by_trip = _assigned_delivery(db, order, short)
    tripless = _driver(db, "+998901005008")
    tripless_order = _order_with_bottles(db, sample_user, sample_product, quantity=8)
    no_trip = _assigned_delivery(db, tripless_order, tripless)

    response = _bulk_mark_in_transit(client, app, admin_user, [covered_by_trip.id, no_trip.id])

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["results"] == {
        "success_count": 1,
        "failed_count": 1,
        "errors": [
            {
                "delivery_id": no_trip.id,
                "error": (
                    f"Order {tripless_order.id} has no bottle-session binding and driver {tripless.id} has no open "
                    "bottle session; open a session to continue delivering this order."
                ),
            }
        ],
        "total_errors": 1,
    }
    db.session.expire_all()
    assert (_status_of(covered_by_trip.id), _status_of(no_trip.id)) == (
        DeliveryStatus.IN_TRANSIT,
        DeliveryStatus.ASSIGNED,
    )
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).one().session_id == session.id
