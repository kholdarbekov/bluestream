"""An office edit of a delivered order moves the driver's trip only by what the edit really changes on it (spec
2026-11-18, calls 7 and 8).

The trip effect is the edit's deltas through the one bottle sum (`trip_units_for_lines`): the 18.9 L bottles, and the
counted one-way units the trip had a line for when the order was delivered. The preview shows it and the apply books
it, from the same reader: a closed trip is reopened, and its tallies added to once, only when the effect is not zero.
An edit that changes nothing on the trip (an unticked product, a line the delivery never reached) leaves a closed trip
closed, even while the driver is on another trip. An edit that must reopen it while the driver is on another trip, his
own or one he co-drives, is refused (409 BOTTLE_SESSION_ACTIVE_CONFLICT), and the preview says so beforehand.

Driven through the routes the admin Orders page calls, with the body Orders.js sends. Only the driver's Telegram
message is recorded instead of sent.
"""

import logging
from datetime import UTC, datetime, timedelta

import pytest

from business_app.models.bottle import BottleLedger, DriverBottleSession, DriverBottleSessionUnit
from business_app.models.order import OrderItem
from business_app.models.product import Product
from business_app.services.notification_service import NotificationService
from shared.enums import DeliveryStatus, DriverBottleSessionStatus, OrderStatus
from tests.integration.reorder_reminder_builders import captured_logs
from tests.integration.trip_unit_builders import (  # noqa: F401  (fixtures)
    big,
    bind,
    delivery_for,
    driver,
    member,
    order_with_lines,
    owner,
    small,
    ten_litre,
    trip,
)
from tests.unit.test_sales_agent_tasks import _spy

pytestmark = pytest.mark.integration

EDIT = "/api/v1/admin/orders/{}/edit"
PREVIEW = "/api/v1/admin/orders/{}/edit-preview"
ADMIN_PRODUCT = "/api/v1/admin/products/{}"

NOW = datetime.now(UTC).replace(microsecond=0)
LINE_MADE = NOW - timedelta(hours=2)
DELIVERED = NOW - timedelta(hours=1)
CLOSED = DriverBottleSessionStatus.CLOSED


@pytest.fixture
def driver_told(monkeypatch):
    """Every staff Telegram message the apply sends (the reopened trip's driver), as `[(user id, text, kwargs)]`."""
    calls = _spy(monkeypatch, NotificationService, "send_staff_telegram_message", result={"success": True})
    return lambda: [(args[1].id, args[2], kwargs) for args, kwargs in calls]


def _reopened_message(session, order):
    """The driver's message for a trip an edit reopened (the test database has no translation row, so the route's
    English text)."""
    return (
        f"Your bottle session #{session.id} was reopened by admin because order #{order.id} was edited after "
        "delivery. Please re-close the session when ready."
    )


def _line(db, session, product, *, delivered=0, returned=None, made=LINE_MADE):
    """The trip's line for `product`, `delivered` already handed over, made at `made`."""
    line = DriverBottleSessionUnit.query.filter_by(session_id=session.id, product_id=product.id).one()
    line.delivered = delivered
    line.returned = returned
    line.discrepancy = None if returned is None else line.loaded - delivered - returned
    line.created_at = made
    db.session.commit()
    return line


def _delivered_order(db, customer, lines, carrier, session, *, delivered_at=DELIVERED):
    """A cash order of `lines`, delivered by `carrier` at `delivered_at` (None: no delivery row), bound to `session`."""
    order = order_with_lines(db, customer, lines, status=OrderStatus.DELIVERED)
    if delivered_at is not None:
        delivery = delivery_for(db, order, carrier, DeliveryStatus.DELIVERED)
        delivery.delivered_at = delivered_at
        delivery.actual_delivery_time = delivered_at
        db.session.commit()
    bind(db, session, order, carrier)
    return order


def _body(order, quantities, reason="Customer changed the order at the door"):
    """What Orders.js sends: every line of the order, with `quantities` ({product id: new quantity}) applied."""
    return {
        "items": [
            {
                "orderItemId": item.id,
                "productId": item.product_id,
                "quantity": quantities.get(item.product_id, item.quantity),
            }
            for item in order.order_items
        ],
        "reason": reason,
    }


def _fresh(db, model, row_id):
    """The row as committed. The test shares the requests' app context, so their teardown never runs: roll back
    here what a refused request flushed and left uncommitted."""
    db.session.rollback()
    return db.session.get(model, row_id)


def _closed_trip_with_ten_litre(db, driver, small, *, delivered, returned):  # noqa: F811
    s = trip(db, driver, bottles=10, units={small.id: 12}, status=CLOSED)
    s.bottles_returned_to_warehouse = 10
    s.discrepancy = 0
    db.session.commit()
    _line(db, s, small, delivered=delivered, returned=returned)
    return s


# --------------------------------------------------------------------------- #
# The apply
# --------------------------------------------------------------------------- #


def test_an_edit_adding_ten_litre_reopens_the_closed_trip_and_adds_to_its_line(
    client, db, admin_claim_headers, driver_told, driver, small, sample_user  # noqa: F811
):
    s = _closed_trip_with_ten_litre(db, driver, small, delivered=4, returned=8)
    order = _delivered_order(db, sample_user, [(small, 4)], driver, s)

    r = client.post(EDIT.format(order.id), json=_body(order, {small.id: 6}), headers=admin_claim_headers)

    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["data"]["cascade_summary"]["session_reopened"] == {
        "bottle_session_id": s.id,
        "bottle_driver_user_id": driver.id,
    }
    s = _fresh(db, DriverBottleSession, s.id)
    assert (s.status, s.reopen_count, s.bottles_delivered, s.bottles_returned_to_warehouse) == (
        DriverBottleSessionStatus.OPEN,
        1,
        0,
        None,
    )
    assert [(line.product_id, line.delivered, line.returned) for line in s.unit_lines] == [(small.id, 6, None)]
    assert BottleLedger.query.filter_by(order_id=order.id).count() == 0
    assert driver_told() == [(driver.id, _reopened_message(s, order), {"language": "en"})]


def test_an_edit_reopens_a_force_closed_trip_and_clears_what_the_office_sealed(
    client, db, admin_claim_headers, driver_told, driver, big, small, sample_user  # noqa: F811
):
    """The office force-closed the trip (10 x 18.9 L and 12 x 10 L loaded, 2 + 4 delivered, everything else back).
    The customer took one more 18.9 L and two more 10 L: the trip is reopened as for a driver's own close, its
    force-close flag and every sealed return and discrepancy cleared, and both deltas tallied."""
    s = trip(db, driver, bottles=10, units={small.id: 12}, status=DriverBottleSessionStatus.FORCE_CLOSED)
    s.bottles_delivered = 2
    s.bottles_returned_to_warehouse = 8
    s.discrepancy = 0
    db.session.commit()
    _line(db, s, small, delivered=4, returned=8)
    order = _delivered_order(db, sample_user, [(big, 2), (small, 4)], driver, s)

    r = client.post(EDIT.format(order.id), json=_body(order, {big.id: 3, small.id: 6}), headers=admin_claim_headers)

    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["data"]["cascade_summary"]["session_reopened"] == {
        "bottle_session_id": s.id,
        "bottle_driver_user_id": driver.id,
    }
    s = _fresh(db, DriverBottleSession, s.id)
    assert (
        s.status,
        s.force_closed,
        s.reopen_count,
        s.bottles_delivered,
        s.bottles_returned_to_warehouse,
        s.discrepancy,
        s.closed_at,
    ) == (DriverBottleSessionStatus.OPEN, False, 1, 3, None, None, None)
    assert [(line.product_id, line.delivered, line.returned, line.discrepancy) for line in s.unit_lines] == [
        (small.id, 6, None, None)
    ]
    assert driver_told() == [(driver.id, _reopened_message(s, order), {"language": "en"})]


def test_an_edit_is_refused_while_the_driver_is_on_a_new_trip_of_his_own(
    client, db, admin_claim_headers, driver_told, driver, big, sample_user  # noqa: F811
):
    """The edit changes the closed trip (one more 18.9 L), so it must reopen it, and the driver already loaded his
    next trip: two open trips of one driver is what `uq_dbs_driver_open` forbids, so the edit is refused with its
    code and nothing moves."""
    s = trip(db, driver, bottles=10, status=CLOSED)
    s.bottles_delivered = 2
    s.bottles_returned_to_warehouse = 8
    s.discrepancy = 0
    db.session.commit()
    new_trip = trip(db, driver, bottles=20)
    order = _delivered_order(db, sample_user, [(big, 2)], driver, s)

    r = client.post(EDIT.format(order.id), json=_body(order, {big.id: 3}), headers=admin_claim_headers)

    assert r.status_code == 409, r.get_data(as_text=True)
    assert r.get_json()["data"]["error_code"] == "BOTTLE_SESSION_ACTIVE_CONFLICT"
    s = _fresh(db, DriverBottleSession, s.id)
    assert (s.status, s.reopen_count, s.bottles_delivered, s.bottles_returned_to_warehouse) == (CLOSED, 0, 2, 8)
    new_trip = _fresh(db, DriverBottleSession, new_trip.id)
    assert (new_trip.status, new_trip.bottles_delivered) == (DriverBottleSessionStatus.OPEN, 0)
    assert [item.quantity for item in OrderItem.query.filter_by(order_id=order.id)] == [2]
    assert BottleLedger.query.filter_by(order_id=order.id).count() == 0
    assert driver_told() == []


def test_a_line_whose_product_row_is_gone_breaks_neither_the_preview_nor_the_apply(
    client, db, admin_claim_headers, driver_told, driver, big, small, sample_user  # noqa: F811
):
    """The order also carries a line whose product row no longer exists, and the client sends only the line it
    changes (the line left out is kept as it is). The trip effect names only the products it moves (one more 10 L), so
    the missing row is never asked for its name."""
    s = trip(db, driver, bottles=10, units={small.id: 12})
    _line(db, s, small, delivered=4)
    order = _delivered_order(db, sample_user, [(big, 2), (small, 4)], driver, s)
    (gone_line,) = [item for item in order.order_items if item.product_id == big.id]
    gone_line.product_id = 987654
    db.session.commit()
    (small_line,) = [item for item in order.order_items if item.product_id == small.id]
    body = {
        "items": [{"orderItemId": small_line.id, "productId": small.id, "quantity": 5}],
        "reason": "Customer took one more",
    }

    preview = client.post(PREVIEW.format(order.id), json=body, headers=admin_claim_headers)
    r = client.post(EDIT.format(order.id), json=body, headers=admin_claim_headers)

    assert preview.status_code == 200, preview.get_data(as_text=True)
    assert preview.get_json()["data"]["cascade_summary"]["trip_effect"] == {
        "session_id": s.id,
        "bottles": 0.0,
        "units": [{"product_id": small.id, "product_name": "10 Litr suv", "delta": 1}],
        "reopens": False,
        "reopen_refusal": None,
    }
    assert r.status_code == 200, r.get_data(as_text=True)
    s = _fresh(db, DriverBottleSession, s.id)
    assert [(line.product_id, line.delivered) for line in s.unit_lines] == [(small.id, 5)]
    assert driver_told() == []


def test_an_edit_lowering_ten_litre_takes_it_off_the_line(
    client, db, admin_claim_headers, driver_told, driver, small, sample_user  # noqa: F811
):
    s = trip(db, driver, units={small.id: 12})
    _line(db, s, small, delivered=4)
    order = _delivered_order(db, sample_user, [(small, 4)], driver, s)

    r = client.post(EDIT.format(order.id), json=_body(order, {small.id: 1}), headers=admin_claim_headers)

    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["data"]["cascade_summary"]["session_reopened"] == {}
    s = _fresh(db, DriverBottleSession, s.id)
    assert (s.status, s.reopen_count) == (DriverBottleSessionStatus.OPEN, 0)
    assert [(line.product_id, line.delivered) for line in s.unit_lines] == [(small.id, 1)]
    assert _fresh(db, Product, small.id).stock_quantity == 1000 + 3
    assert driver_told() == []


def test_an_edit_that_changes_nothing_on_the_trip_leaves_it_closed_even_while_the_driver_is_on_a_new_trip(
    client, db, admin_claim_headers, driver_told, driver, big, small, sample_user, sample_category  # noqa: F811
):
    """The trip setting is off for this one-way product: it never rode on a trip, so its quantity is no business of
    the driver's closed trip, and the trip he is on now does not stand in the way."""
    unticked = ten_litre(db, sample_category, counted=False)
    s = _closed_trip_with_ten_litre(db, driver, small, delivered=0, returned=12)
    new_trip = trip(db, driver, bottles=10)
    order = _delivered_order(db, sample_user, [(big, 2), (unticked, 2)], driver, s)

    r = client.post(EDIT.format(order.id), json=_body(order, {unticked.id: 5}), headers=admin_claim_headers)

    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["data"]["cascade_summary"]["session_reopened"] == {}
    s = _fresh(db, DriverBottleSession, s.id)
    assert (s.status, s.reopen_count, s.bottles_returned_to_warehouse) == (CLOSED, 0, 10)
    assert [(line.delivered, line.returned) for line in s.unit_lines] == [(0, 12)]
    new_trip = _fresh(db, DriverBottleSession, new_trip.id)
    assert (new_trip.status, new_trip.bottles_delivered) == (DriverBottleSessionStatus.OPEN, 0)
    assert [item.quantity for item in OrderItem.query.filter_by(order_id=order.id, product_id=unticked.id)] == [5]
    assert driver_told() == []


def test_an_edit_is_refused_while_the_driver_co_drives_another_trip(
    client, db, admin_claim_headers, driver_told, driver, owner, big, sample_user  # noqa: F811
):
    s = trip(db, driver, bottles=10, status=CLOSED)
    s.bottles_delivered = 2
    db.session.commit()
    owners_trip = trip(db, owner, bottles=10)
    member(db, owners_trip, driver)
    order = _delivered_order(db, sample_user, [(big, 2)], driver, s)

    r = client.post(EDIT.format(order.id), json=_body(order, {big.id: 3}), headers=admin_claim_headers)

    assert r.status_code == 409, r.get_data(as_text=True)
    assert r.get_json()["data"]["error_code"] == "BOTTLE_SESSION_ACTIVE_CONFLICT"
    s = _fresh(db, DriverBottleSession, s.id)
    assert (s.status, s.reopen_count, s.bottles_delivered) == (CLOSED, 0, 2)
    assert _fresh(db, DriverBottleSession, owners_trip.id).bottles_delivered == 0
    assert [item.quantity for item in OrderItem.query.filter_by(order_id=order.id)] == [2]
    assert BottleLedger.query.filter_by(order_id=order.id).count() == 0
    assert driver_told() == []


def test_a_ten_litre_edit_of_an_order_delivered_before_its_trip_had_a_line_neither_reopens_nor_is_refused(
    client, db, admin_claim_headers, driver_told, driver, small, sample_user  # noqa: F811
):
    """The line was made after the order was delivered (the setting was ticked later), so the Delivered never put
    these 10 L on it, and an edit of them must not either."""
    s = trip(db, driver, units={small.id: 5}, status=CLOSED)
    _line(db, s, small, delivered=0, returned=5, made=DELIVERED + timedelta(minutes=30))
    trip(db, driver, bottles=10)  # the trip he is on now
    order = _delivered_order(db, sample_user, [(small, 4)], driver, s)
    body = _body(order, {small.id: 6})

    preview = client.post(PREVIEW.format(order.id), json=body, headers=admin_claim_headers)
    r = client.post(EDIT.format(order.id), json=body, headers=admin_claim_headers)

    assert preview.status_code == 200, preview.get_data(as_text=True)
    assert preview.get_json()["data"]["cascade_summary"]["trip_effect"] == {
        "session_id": s.id,
        "bottles": 0.0,
        "units": [],
        "reopens": False,
        "reopen_refusal": None,
    }
    assert r.status_code == 200, r.get_data(as_text=True)
    s = _fresh(db, DriverBottleSession, s.id)
    assert (s.status, s.reopen_count) == (CLOSED, 0)
    assert [(line.delivered, line.returned) for line in s.unit_lines] == [(0, 5)]
    assert driver_told() == []


def test_an_edit_after_the_office_unticked_the_product_still_corrects_the_trip_that_counted_it(
    client, db, admin_claim_headers, driver_told, driver, small, sample_user  # noqa: F811
):
    """4 x 10 L were delivered on the trip, which counted them on its 10 L line, and the trip is closed (8 back). The
    office then unticks "counted on the bottle trip" (allowed: no trip is open) and corrects the order to 1. The trip's
    line is what counted those 4, so the edit corrects that line whatever the setting says now: the trip reopens and
    its 10 L delivered is 1. Read through today's setting, the edit moved nothing and the sealed line kept 4 for a
    delivery of 1."""
    s = _closed_trip_with_ten_litre(db, driver, small, delivered=4, returned=8)
    order = _delivered_order(db, sample_user, [(small, 4)], driver, s)
    unticked = client.put(
        ADMIN_PRODUCT.format(small.id), json={"counted_on_bottle_trip": False}, headers=admin_claim_headers
    )
    assert unticked.status_code == 200, unticked.get_data(as_text=True)
    body = _body(order, {small.id: 1})

    preview = client.post(PREVIEW.format(order.id), json=body, headers=admin_claim_headers)
    r = client.post(EDIT.format(order.id), json=body, headers=admin_claim_headers)

    assert preview.status_code == 200, preview.get_data(as_text=True)
    assert preview.get_json()["data"]["cascade_summary"]["trip_effect"] == {
        "session_id": s.id,
        "bottles": 0.0,
        "units": [{"product_id": small.id, "product_name": "10 Litr suv", "delta": -3}],
        "reopens": True,
        "reopen_refusal": None,
    }
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["data"]["cascade_summary"]["session_reopened"] == {
        "bottle_session_id": s.id,
        "bottle_driver_user_id": driver.id,
    }
    s = _fresh(db, DriverBottleSession, s.id)
    assert (s.status, s.reopen_count) == (DriverBottleSessionStatus.OPEN, 1)
    assert [(line.product_id, line.delivered, line.returned) for line in s.unit_lines] == [(small.id, 1, None)]
    assert _fresh(db, Product, small.id).counted_on_bottle_trip is False
    assert driver_told() == [(driver.id, _reopened_message(s, order), {"language": "en"})]


def test_a_ten_litre_edit_of_an_order_with_no_delivery_time_leaves_the_line_and_logs_it(
    client, db, caplog, admin_claim_headers, driver, small, sample_user  # noqa: F811
):
    """No delivery row, so nothing says whether the trip had its line when the order was delivered."""
    s = trip(db, driver, units={small.id: 12})
    _line(db, s, small, delivered=4)
    order = _delivered_order(db, sample_user, [(small, 4)], driver, s, delivered_at=None)

    with captured_logs(caplog, logging.getLogger("business_app.services.order_edit_service"), logging.WARNING):
        r = client.post(EDIT.format(order.id), json=_body(order, {small.id: 6}), headers=admin_claim_headers)

    assert r.status_code == 200, r.get_data(as_text=True)
    s = _fresh(db, DriverBottleSession, s.id)
    assert [line.delivered for line in s.unit_lines] == [4]
    assert f"[BOTTLE] order={order.id} has no delivery time; units {{{small.id}: 2}} not put on trip={s.id}" in (
        caplog.messages
    )


# --------------------------------------------------------------------------- #
# The preview
# --------------------------------------------------------------------------- #


def test_the_preview_shows_the_trip_effect(
    client, db, admin_claim_headers, driver, big, small, sample_user  # noqa: F811
):
    s = _closed_trip_with_ten_litre(db, driver, small, delivered=4, returned=8)
    order = _delivered_order(db, sample_user, [(big, 2), (small, 4)], driver, s)

    r = client.post(PREVIEW.format(order.id), json=_body(order, {big.id: 3, small.id: 6}), headers=admin_claim_headers)

    assert r.status_code == 200, r.get_data(as_text=True)
    summary = r.get_json()["data"]["cascade_summary"]
    assert summary["trip_effect"] == {
        "session_id": s.id,
        "bottles": 1.0,
        "units": [{"product_id": small.id, "product_name": "10 Litr suv", "delta": 2}],
        "reopens": True,
        "reopen_refusal": None,
    }
    assert summary["bottle_balance"] == {"changes": [{"product_id": big.id, "delta_bottles": 1.0}]}
    s = _fresh(db, DriverBottleSession, s.id)
    assert (s.status, s.reopen_count) == (CLOSED, 0)  # a preview writes nothing


def test_the_preview_of_an_edit_on_an_open_trip_reopens_nothing(
    client, db, admin_claim_headers, driver, big, sample_user  # noqa: F811
):
    s = trip(db, driver, bottles=10)
    order = _delivered_order(db, sample_user, [(big, 2)], driver, s)

    r = client.post(PREVIEW.format(order.id), json=_body(order, {big.id: 3}), headers=admin_claim_headers)

    assert r.status_code == 200, r.get_data(as_text=True)
    summary = r.get_json()["data"]["cascade_summary"]
    assert summary["trip_effect"] == {
        "session_id": s.id,
        "bottles": 1.0,
        "units": [],
        "reopens": False,
        "reopen_refusal": None,
    }
    assert summary["bottle_balance"] == {"changes": [{"product_id": big.id, "delta_bottles": 1.0}]}


def test_the_preview_says_the_edit_will_be_refused_while_the_driver_co_drives_another_trip(
    client, db, admin_claim_headers, driver, owner, big, sample_user  # noqa: F811
):
    s = trip(db, driver, bottles=10, status=CLOSED)
    member(db, trip(db, owner, bottles=10), driver)
    order = _delivered_order(db, sample_user, [(big, 2)], driver, s)
    body = _body(order, {big.id: 3})

    preview = client.post(PREVIEW.format(order.id), json=body, headers=admin_claim_headers)
    r = client.post(EDIT.format(order.id), json=body, headers=admin_claim_headers)

    assert preview.status_code == 200, preview.get_data(as_text=True)
    data = preview.get_json()["data"]
    assert data["cascade_summary"]["trip_effect"] == {
        "session_id": s.id,
        "bottles": 1.0,
        "units": [],
        "reopens": True,
        "reopen_refusal": "BOTTLE_SESSION_ACTIVE_CONFLICT",
    }
    assert data["blocking_reasons"] == []  # the apply answers it, with its code
    assert (r.status_code, r.get_json()["data"]["error_code"]) == (409, "BOTTLE_SESSION_ACTIVE_CONFLICT")


def test_the_preview_of_an_edit_before_delivery_puts_nothing_on_the_trip(
    client, db, admin_claim_headers, driver, big, small, sample_user  # noqa: F811
):
    """The Delivered tallies the order's quantities as they are then, so an edit before it changes no trip."""
    s = trip(db, driver, bottles=10, units={small.id: 12})
    order = order_with_lines(db, sample_user, [(big, 2), (small, 4)], status=OrderStatus.OUT_FOR_DELIVERY)
    delivery_for(db, order, driver, DeliveryStatus.IN_TRANSIT)
    bind(db, s, order, driver)

    r = client.post(PREVIEW.format(order.id), json=_body(order, {big.id: 3, small.id: 6}), headers=admin_claim_headers)

    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["data"]["cascade_summary"]["trip_effect"] == {
        "session_id": s.id,
        "bottles": 0.0,
        "units": [],
        "reopens": False,
        "reopen_refusal": None,
    }
