"""Accept, auto-assign and admin assignment judge EVERY line of an order against the crew's trip (spec 2026-11-18 §6.4).

An order's lines are its 18.9 L bottles and each counted one-way unit product (10 L). The bot's accept and
auto-assign need a trip for any of them (a 10 L-only order included, whatever the strict flag) and refuse the first
short line; the admin routes never refuse, they bind only when every line is covered.

Driven through the routes the staff bot and the admin page call, with their bodies. Only external I/O is faked.
"""

import pytest

from business_app.models.bottle import DriverBottleSessionOrder
from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.tasks.delivery_tasks import auto_assign_delivery_task
from shared.enums import DeliveryStatus
from tests.integration.test_redispatch_reschedules_to_today import enqueued  # noqa: F401  (fixture)
from tests.integration.trip_unit_builders import (  # noqa: F401  (fixtures)
    bind,
    big,
    delivery_for,
    driver,
    order_with_lines,
    owner,
    small,
    staff_headers,
    strict,
    trip,
)

pytestmark = pytest.mark.integration

ACCEPT = "/api/v1/staff/delivery/accept/{}"
REASSIGN = "/api/v1/admin/staff/delivery/reassign/{}"
NO_DRIVER = "no_driver_with_enough_bottles"


def _accept(client, driver, delivery):
    return client.post(ACCEPT.format(delivery.id), headers=staff_headers(driver))


@pytest.mark.parametrize("on", [True, False])
def test_a_ten_litre_only_order_needs_a_trip_at_accept(app, client, db, monkeypatch, driver, small, sample_user, on):
    strict(app, monkeypatch, on)
    order = order_with_lines(db, sample_user, [(small, 4)])
    d = delivery_for(db, order, None, DeliveryStatus.SCHEDULED)

    r = _accept(client, driver, d)

    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.get_json()["error_code"] == "BOTTLE_SESSION_REQUIRED"
    assert Delivery.query.get(d.id).delivery_person_id is None


def test_a_mixed_order_short_only_on_ten_litre_is_refused_on_that_line(
    app, client, db, monkeypatch, driver, big, small, sample_user
):
    strict(app, monkeypatch, True)
    trip(db, driver, bottles=40, units={small.id: 6})
    on_the_way = order_with_lines(db, sample_user, [(small, 4)])
    delivery_for(db, on_the_way, driver, DeliveryStatus.IN_TRANSIT)
    order = order_with_lines(db, sample_user, [(big, 3), (small, 4)])
    d = delivery_for(db, order, None, DeliveryStatus.SCHEDULED)

    r = _accept(client, driver, d)

    assert r.status_code == 400, r.get_data(as_text=True)
    body = r.get_json()
    assert body["error_code"] == "BOTTLE_SESSION_CAPACITY_EXCEEDED"
    assert body["details"] == {
        "product_id": small.id,
        "product_name": "10 Litr suv",
        "available": 2,
        "required": 4,
        "shortfall": 2,
        "validation_errors": [],
    }
    assert Delivery.query.get(d.id).delivery_person_id is None


def test_a_mixed_order_covered_on_both_lines_binds(enqueued, app, client, db, monkeypatch, driver, big, small, sample_user):
    strict(app, monkeypatch, True)
    session = trip(db, driver, bottles=40, units={small.id: 6})
    # Decoys so the accepted delivery's id (2) and its history row's id (1+) cannot coincide on fresh tables.
    decoy = delivery_for(db, order_with_lines(db, sample_user, [(big, 1)]), None, DeliveryStatus.SCHEDULED)
    db.session.add(DeliveryStatusHistory(delivery_id=decoy.id, old_status=DeliveryStatus.SCHEDULED, new_status=DeliveryStatus.SCHEDULED))
    db.session.add(DeliveryStatusHistory(delivery_id=decoy.id, old_status=DeliveryStatus.SCHEDULED, new_status=DeliveryStatus.SCHEDULED))
    db.session.commit()
    order = order_with_lines(db, sample_user, [(big, 3), (small, 4)])
    d = delivery_for(db, order, None, DeliveryStatus.SCHEDULED)

    r = _accept(client, driver, d)

    assert r.status_code == 200, r.get_data(as_text=True)
    assert Delivery.query.get(d.id).delivery_person_id == driver.id
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).one().session_id == session.id
    # The task takes the DeliveryStatusHistory id, not the delivery id (the two only coincide on fresh tables).
    history_id = DeliveryStatusHistory.query.filter_by(delivery_id=d.id).one().id
    assert history_id != d.id
    assert enqueued == [("business_app.tasks.notification_tasks.send_delivery_update_task", (history_id,), {})]


def test_a_reopened_trip_refuses_any_ten_litre(app, client, db, monkeypatch, driver, small, sample_user):
    from datetime import datetime, timezone

    strict(app, monkeypatch, True)
    session = trip(db, driver, units={small.id: 6})
    session.reopened_at = datetime.now(timezone.utc)
    db.session.commit()
    order = order_with_lines(db, sample_user, [(small, 1)])
    d = delivery_for(db, order, None, DeliveryStatus.SCHEDULED)

    r = _accept(client, driver, d)

    assert r.status_code == 400, r.get_data(as_text=True)
    body = r.get_json()
    assert body["error_code"] == "BOTTLE_SESSION_CAPACITY_EXCEEDED"
    assert {k: body["details"][k] for k in ("product_id", "available", "required", "shortfall")} == {
        "product_id": small.id,
        "available": 0,
        "required": 1,
        "shortfall": 1,
    }


def test_auto_assign_skips_a_crew_short_on_ten_litre(enqueued, db, driver, owner, small, sample_user):
    trip(db, driver, units={small.id: 2})
    trip(db, owner, units={small.id: 6})
    order = order_with_lines(db, sample_user, [(small, 4)])
    d = delivery_for(db, order, None, DeliveryStatus.SCHEDULED)

    result = auto_assign_delivery_task.run(d.id)

    assert (result["success"], result["driver_id"]) == (True, owner.id)
    assert Delivery.query.get(d.id).delivery_person_id == owner.id
    assert enqueued == [
        ("business_app.tasks.notification_tasks.notify_driver_assignment_task", (d.id,), {}),
        ("business_app.tasks.delivery_tasks.optimize_driver_route_task", (owner.id, "accept"), {}),
    ]


def test_auto_assign_finds_nobody_when_every_crew_is_short_on_ten_litre(enqueued, db, driver, owner, small, sample_user):
    trip(db, driver, units={small.id: 2})
    trip(db, owner, units={small.id: 3})
    order = order_with_lines(db, sample_user, [(small, 4)])
    d = delivery_for(db, order, None, DeliveryStatus.SCHEDULED)

    result = auto_assign_delivery_task.run(d.id)

    assert result == {"success": False, "delivery_id": d.id, "error": NO_DRIVER}
    assert enqueued == []
    assert Delivery.query.get(d.id).delivery_person_id is None


def test_admin_assign_binds_only_when_every_line_is_covered(
    enqueued, client, db, admin_claim_headers, driver, owner, big, small, sample_user
):
    a_trip = trip(db, driver, bottles=40, units={small.id: 6})
    b_trip = trip(db, owner, bottles=40, units={small.id: 6})
    order = order_with_lines(db, sample_user, [(big, 3), (small, 4)])
    d = delivery_for(db, order, driver, DeliveryStatus.ASSIGNED)
    bind(db, a_trip, order, driver)

    covered = client.put(REASSIGN.format(d.id), headers=admin_claim_headers, json={"new_delivery_person_id": owner.id})

    assert covered.status_code == 200, covered.get_data(as_text=True)
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).one().session_id == b_trip.id
    assert [c[0] for c in enqueued] == ["staff.notify_order_reassigned"]
    enqueued.clear()

    # Back to the first crew once it is short on 10 L only: the order is moved, never refused, and not bound there.
    from business_app.models.bottle import DriverBottleSessionUnit

    DriverBottleSessionUnit.query.filter_by(session_id=a_trip.id).one().loaded = 3
    db.session.commit()

    short = client.put(REASSIGN.format(d.id), headers=admin_claim_headers, json={"new_delivery_person_id": driver.id})

    assert short.status_code == 200, short.get_data(as_text=True)
    assert Delivery.query.get(d.id).delivery_person_id == driver.id
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).first() is None
    assert [c[0] for c in enqueued] == ["staff.notify_order_reassigned"]
