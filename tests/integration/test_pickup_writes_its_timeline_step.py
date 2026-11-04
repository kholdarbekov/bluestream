"""The driver's pickup is a step of the customer's timeline.

The pickup moves the order to out_for_delivery (`DELIVERY_TO_ORDER_STATUS_SYNC`). Track's timeline reads only
`OrderStatusHistory`, so the pickup has to write a row there, or the screen still says "Order Confirmed <- Current"
above "Arriving within about N min".

Driven through the staff bot's own status route and read back through the customer's own Track route.
"""

import pytest

from business_app.models.order import OrderStatusHistory
from shared.enums import DeliveryStatus, OrderStatus
from staff_bot.api_client import StaffAPIClient
from tests.integration.test_redispatch_reschedules_to_today import enqueued  # noqa: F401 -- a fixture
from tests.integration.test_staff_delivery_ownership_guard import _bearer, _delivery, _driver

pytestmark = pytest.mark.integration

STATUS = "/api/v1/staff/delivery/{}/status"
TRACK = "/api/v1/orders/{}/track"


def _taps(app, client, driver, delivery, status, metadata=None):
    response = client.put(
        STATUS.format(delivery.id),
        json=StaffAPIClient.delivery_status_body(status, metadata),
        headers=_bearer(app, driver),
    )
    assert response.status_code == 200, response.get_data(as_text=True)


def _timeline(client, auth_headers, delivery):
    response = client.get(TRACK.format(delivery.order_id), headers=auth_headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return [(step["status"], step["is_current"]) for step in response.get_json()["data"]["timeline"]]


def _assigned_stop(db, customer, number):
    driver = _driver(db, phone="+998901360001", first_name="Pickup")
    return driver, _delivery(db, customer, driver, status=DeliveryStatus.ASSIGNED, number=number)


def test_the_pickup_is_the_current_step(app, client, db, sample_user, auth_headers, enqueued):  # noqa: F811
    driver, delivery = _assigned_stop(db, sample_user, "ORD-STEP-CURRENT")
    assert delivery.order.status == OrderStatus.CONFIRMED

    _taps(app, client, driver, delivery, "picked_up")

    assert _timeline(client, auth_headers, delivery) == [("created", False), ("out_for_delivery", True)]
    [row] = OrderStatusHistory.query.filter_by(order_id=delivery.order_id).all()
    assert (row.old_status, row.new_status, row.changed_by) == (
        OrderStatus.CONFIRMED,
        OrderStatus.OUT_FOR_DELIVERY,
        driver.id,
    )


def test_the_step_carries_no_note_for_the_customer_to_read(
    app, client, db, sample_user, auth_headers, enqueued  # noqa: F811
):
    """Track prints a step's notes. The delivery's own row holds the staff wording ("Status updated via staff bot to
    picked_up"), in English, and that is not for the customer."""
    driver, delivery = _assigned_stop(db, sample_user, "ORD-STEP-NOTES")

    _taps(app, client, driver, delivery, "picked_up", {"notes": "Back door, ask for the guard"})

    response = client.get(TRACK.format(delivery.order_id), headers=auth_headers)
    assert [step["notes"] for step in response.get_json()["data"]["timeline"]] == [None, None]


def test_setting_off_and_arriving_keep_it_the_current_step(
    app, client, db, sample_user, auth_headers, enqueued  # noqa: F811
):
    driver, delivery = _assigned_stop(db, sample_user, "ORD-STEP-ON-THE-WAY")

    _taps(app, client, driver, delivery, "picked_up")
    _taps(app, client, driver, delivery, "in_transit")
    _taps(app, client, driver, delivery, "arrived")

    assert _timeline(client, auth_headers, delivery) == [("created", False), ("out_for_delivery", True)]


def test_an_order_already_out_for_delivery_gets_no_second_step(
    app, client, db, sample_user, auth_headers, enqueued  # noqa: F811
):
    """An admin can move an order to out_for_delivery by hand, with its own row, before a driver picks it up."""
    driver, delivery = _assigned_stop(db, sample_user, "ORD-STEP-BY-HAND")
    delivery.order.status = OrderStatus.OUT_FOR_DELIVERY
    db.session.add(
        OrderStatusHistory(
            order_id=delivery.order_id, old_status=OrderStatus.PREPARING, new_status=OrderStatus.OUT_FOR_DELIVERY
        )
    )
    db.session.commit()

    _taps(app, client, driver, delivery, "picked_up")

    assert _timeline(client, auth_headers, delivery) == [("created", False), ("out_for_delivery", True)]
