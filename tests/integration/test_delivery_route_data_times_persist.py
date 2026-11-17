"""The step times written into `Delivery.route_data` are saved (owner request 2026-11-17: "Fix the lost times too").

`route_data` was a plain JSON column. The three writers did `delivery.route_data = delivery.route_data or {}` and then
set a key on that same dict, so SQLAlchemy saw no change and a key added to a non-empty `route_data` was never stored.

Every case drives the real HTTP route with the real client's body and reads `route_data` back through a FRESH session
(`db.session.remove()` then a new query): the identity map would otherwise show the unsaved change. Only the Celery
publishes are mocked.
"""

from datetime import UTC, datetime, timedelta

import pytest

from business_app.models.delivery import Delivery
from business_app.tasks.delivery_tasks import optimize_driver_route_task
from business_app.tasks.notification_tasks import send_delivery_update_task
from shared.enums import DeliveryStatus
from staff_bot.api_client import StaffAPIClient
from tests.integration.test_assign_free_full_api import _admin_headers
from tests.integration.test_auto_arrival_from_driver_location import _share, _stop
from tests.unit.test_staff_error_endpoints import _assigned_delivery, _driver, _headers, _order_with_bottles

from unittest.mock import patch

pytestmark = pytest.mark.integration

STATUS = "/api/v1/staff/delivery/{}/status"
BULK_ACTIONS = "/api/v1/admin/bulk-actions"
START_DELIVERY = "/api/v1/delivery/driver/start-delivery/{}"
ASSIGNED_AT = "2026-11-17T08:00:00+00:00"


@pytest.fixture(autouse=True)
def publishes():
    with (
        patch.object(send_delivery_update_task, "delay"),
        patch.object(optimize_driver_route_task, "delay"),
    ):
        yield


def _fresh_route_data(db, delivery_id):
    """`route_data` as the database holds it: a new session, no identity map."""
    db.session.remove()
    return dict(db.session.get(Delivery, delivery_id).route_data or {})


def _seed_keys(db, delivery_id):
    """A real delivery already holds keys when the driver starts (assignment writes `assigned_at`)."""
    delivery = db.session.get(Delivery, delivery_id)
    delivery.route_data = {"assigned_at": ASSIGNED_AT}
    db.session.commit()


def _staff_delivery(db, sample_user, sample_product, phone="+998901007001"):
    driver = _driver(db, phone)
    order = _order_with_bottles(db, sample_user, sample_product, quantity=1)
    delivery = _assigned_delivery(db, order, driver)
    _seed_keys(db, delivery.id)
    return driver, delivery.id


def test_the_button_path_saves_every_step_time(app, client, db, sample_user, sample_product):
    driver, delivery_id = _staff_delivery(db, sample_user, sample_product)
    seen = {"assigned_at": ASSIGNED_AT}

    for status, key in (("picked_up", "picked_up_at"), ("in_transit", "in_transit_at"), ("arrived", "arrived_at")):
        response = client.put(
            STATUS.format(delivery_id),
            json=StaffAPIClient.delivery_status_body(status),
            headers=_headers(app, driver.id),
        )
        assert response.status_code == 200, (status, response.get_json())
        stored = _fresh_route_data(db, delivery_id)
        assert key in stored, (status, stored)
        assert all(stored[k] == v for k, v in seen.items()), (status, stored)
        seen = stored

    assert set(seen) == {"assigned_at", "picked_up_at", "in_transit_at", "arrived_at"}
    times = [datetime.fromisoformat(seen[k]) for k in ("picked_up_at", "in_transit_at", "arrived_at")]
    assert times == sorted(times)


def test_an_automatic_arrival_saves_arrived_at(client, db, driver_auth_headers, driver_with_location):
    delivery_id = _stop(db, driver_with_location, 50)
    _seed_keys(db, delivery_id)

    response = _share(client, driver_auth_headers)

    assert response.status_code == 200
    assert db.session.get(Delivery, delivery_id).status == DeliveryStatus.ARRIVED
    stored = _fresh_route_data(db, delivery_id)
    assert stored["assigned_at"] == ASSIGNED_AT
    assert "arrived_at" in stored, stored


def test_the_admin_bulk_mark_in_transit_saves_its_times(app, client, db, admin_user, sample_user, sample_product):
    _, delivery_id = _staff_delivery(db, sample_user, sample_product, "+998901007002")

    response = client.post(
        BULK_ACTIONS,
        json={"action": "mark_in_transit", "target_type": "delivery", "target_ids": [delivery_id]},
        headers=_admin_headers(app, admin_user),
    )

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["results"]["success_count"] == 1, response.get_json()
    stored = _fresh_route_data(db, delivery_id)
    assert stored["assigned_at"] == ASSIGNED_AT
    assert {"picked_up_at", "in_transit_at"} <= set(stored), stored


def test_the_driver_start_delivery_route_saves_its_times(app, client, db, sample_user, sample_product):
    driver, delivery_id = _staff_delivery(db, sample_user, sample_product, "+998901007003")

    response = client.post(START_DELIVERY.format(delivery_id), headers=_headers(app, driver.id))

    assert response.status_code == 200, response.get_json()
    stored = _fresh_route_data(db, delivery_id)
    assert {"assigned_at", "picked_up_at", "in_transit_at"} <= set(stored), stored


def _pending_delivery(db, sample_user, sample_product, driver):
    """A delivery added to the session but not flushed. `_order_with_bottles` only flushes, so nothing commits here."""
    order = _order_with_bottles(db, sample_user, sample_product, 1)
    delivery = Delivery(
        order_id=order.id,
        status=DeliveryStatus.ASSIGNED,
        delivery_person_id=driver.id,
        scheduled_date=datetime.now(UTC) + timedelta(hours=1),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    return delivery


def test_deliveries_never_share_a_route_data_object(app, db, sample_user, sample_product):
    """R2: `default={}` hands one dict to every insert; a key written on one must not reach another.

    The drivers come first because `_driver` commits. Both deliveries are then flushed together with NO commit
    before the identity check: a commit expires them, and the next read would load a fresh dict per row and hide
    the sharing.
    """
    drivers = [_driver(db, phone) for phone in ("+998901007004", "+998901007005", "+998901007006")]
    first = _pending_delivery(db, sample_user, sample_product, drivers[0])
    second = _pending_delivery(db, sample_user, sample_product, drivers[1])
    db.session.flush()

    assert first.route_data is not second.route_data
    first.route_data["leak"] = "x"
    assert second.route_data == {}
    db.session.commit()

    third = _pending_delivery(db, sample_user, sample_product, drivers[2])
    db.session.commit()
    ids = (first.id, second.id, third.id)

    assert [_fresh_route_data(db, i) for i in ids] == [{"leak": "x"}, {}, {}]


def test_each_insert_gets_its_own_new_route_data_dict():
    """R1 `default=dict`. MutableDict copies the default's dict, so the insert test above passes with `default={}`
    too; only the column default itself shows whether one dict is shared again."""
    default = Delivery.__table__.c.route_data.default

    assert default.is_callable
    a, b = default.arg(None), default.arg(None)
    assert a == b == {}
    assert a is not b
