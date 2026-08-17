"""Loading bottles / returning to warehouse anchors the driver AT the warehouse.

Both actions physically happen at the single depot, so they are as good a
position fix as a shared pin — and a better one than the stale reading the
driver left behind yesterday. Driven through HTTP because four separate
endpoints (two current, two deprecated shims) funnel into the same two service
methods, and the whole point of anchoring in the service layer is that all four
inherit it.
"""

from datetime import datetime, timedelta, timezone

import pytest

from business_app.models.delivery import DeliveryPerson

pytestmark = pytest.mark.integration


# Deliberately NOT the WAREHOUSE_LATITUDE/LONGITUDE defaults (41.2995/69.2401,
# the Tashkent-centre constant) and not `driver_with_location`'s 41.30/69.24 —
# a test that asserted the defaults would pass even if the code ignored the
# config and hard-coded the city centre.
WAREHOUSE_LAT = 41.4111
WAREHOUSE_LNG = 69.5222

OPEN_URL = "/api/v1/staff/bottles/session/open"
CLOSE_URL = "/api/v1/staff/bottles/session/close"
LEGACY_LOAD_URL = "/api/v1/staff/bottles/load"
LEGACY_RETURN_URL = "/api/v1/staff/bottles/return-to-warehouse"
LOCATION_URL = "/api/v1/staff/delivery/me/location"


def _person(driver):
    return DeliveryPerson.query.filter_by(user_id=driver.id).first()


@pytest.fixture
def warehouse_coords(app, monkeypatch):
    monkeypatch.setitem(app.config, "WAREHOUSE_LATITUDE", WAREHOUSE_LAT)
    monkeypatch.setitem(app.config, "WAREHOUSE_LONGITUDE", WAREHOUSE_LNG)
    return (WAREHOUSE_LAT, WAREHOUSE_LNG)


@pytest.fixture
def driver_person_without_location(db, delivery_driver):
    """A driver with a real DeliveryPerson row but no position ever shared —
    `location_status == "missing"`, the state a brand-new driver starts in."""
    person = DeliveryPerson(
        user_id=delivery_driver.id,
        full_name="Depot Driver",
        phone="+998901112244",
        is_active=True,
        is_available=True,
    )
    db.session.add(person)
    db.session.commit()
    return delivery_driver


def _share_pin(client, headers, lat, lng, accuracy=None):
    payload = {"latitude": lat, "longitude": lng}
    if accuracy is not None:
        payload["horizontal_accuracy"] = accuracy
    resp = client.post(LOCATION_URL, json=payload, headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# Load bottles
# --------------------------------------------------------------------------- #


def test_loading_bottles_moves_the_driver_to_the_warehouse(
    client, db, driver_auth_headers, delivery_driver, driver_with_location, warehouse_coords
):
    before = _person(delivery_driver).last_location_update

    resp = client.post(OPEN_URL, json={"bottles_loaded": 20}, headers=driver_auth_headers)

    assert resp.status_code == 201, resp.get_data(as_text=True)
    db.session.expire_all()
    person = _person(delivery_driver)
    assert person.current_location_lat == pytest.approx(WAREHOUSE_LAT)
    assert person.current_location_lng == pytest.approx(WAREHOUSE_LNG)
    assert person.last_location_update > before


def test_loading_bottles_clears_the_measured_accuracy_radius(
    client, db, driver_auth_headers, delivery_driver, driver_with_location, warehouse_coords
):
    """A warehouse anchor is derived, not measured. Leaving the previous fix's
    20m radius attached would advertise precision for a point no GPS produced.
    """
    _share_pin(client, driver_auth_headers, 41.31, 69.28, accuracy=20.0)

    client.post(OPEN_URL, json={"bottles_loaded": 20}, headers=driver_auth_headers)

    db.session.expire_all()
    assert _person(delivery_driver).location_accuracy_m is None


def test_deprecated_load_shim_also_anchors_at_the_warehouse(
    client, db, driver_auth_headers, delivery_driver, driver_with_location, warehouse_coords
):
    """Proof the anchor lives in the service, not in one route handler."""
    resp = client.post(LEGACY_LOAD_URL, json={"bottles_loaded": 12}, headers=driver_auth_headers)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    db.session.expire_all()
    person = _person(delivery_driver)
    assert person.current_location_lat == pytest.approx(WAREHOUSE_LAT)
    assert person.current_location_lng == pytest.approx(WAREHOUSE_LNG)


# --------------------------------------------------------------------------- #
# Return to warehouse
# --------------------------------------------------------------------------- #


def test_returning_to_warehouse_moves_the_driver_back_to_the_warehouse(
    client, db, driver_auth_headers, delivery_driver, driver_with_location, warehouse_coords
):
    client.post(OPEN_URL, json={"bottles_loaded": 20}, headers=driver_auth_headers)
    # Drive away, so the close is what puts the driver back — not a leftover
    # anchor from the open.
    _share_pin(client, driver_auth_headers, 41.35, 69.31, accuracy=15.0)

    resp = client.post(
        CLOSE_URL, json={"bottles_returned_to_warehouse": 5}, headers=driver_auth_headers
    )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    db.session.expire_all()
    person = _person(delivery_driver)
    assert person.current_location_lat == pytest.approx(WAREHOUSE_LAT)
    assert person.current_location_lng == pytest.approx(WAREHOUSE_LNG)
    assert person.location_accuracy_m is None


def test_deprecated_return_shim_also_anchors_at_the_warehouse(
    client, db, driver_auth_headers, delivery_driver, driver_with_location, warehouse_coords
):
    client.post(OPEN_URL, json={"bottles_loaded": 20}, headers=driver_auth_headers)
    _share_pin(client, driver_auth_headers, 41.35, 69.31)

    resp = client.post(
        LEGACY_RETURN_URL,
        json={"bottles_returned_to_warehouse": 5},
        headers=driver_auth_headers,
    )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    db.session.expire_all()
    person = _person(delivery_driver)
    assert person.current_location_lat == pytest.approx(WAREHOUSE_LAT)
    assert person.current_location_lng == pytest.approx(WAREHOUSE_LNG)


# --------------------------------------------------------------------------- #
# Actions that must NOT move the driver
# --------------------------------------------------------------------------- #


def test_rejected_open_leaves_the_previous_fix_intact(
    client, db, driver_auth_headers, delivery_driver, driver_with_location, warehouse_coords
):
    """The anchor rides the session's transaction. A second open is a 409, so
    the driver must not be teleported by an action that did not happen."""
    client.post(OPEN_URL, json={"bottles_loaded": 20}, headers=driver_auth_headers)
    _share_pin(client, driver_auth_headers, 41.35, 69.31, accuracy=15.0)
    db.session.expire_all()
    previous_update = _person(delivery_driver).last_location_update

    resp = client.post(OPEN_URL, json={"bottles_loaded": 9}, headers=driver_auth_headers)

    assert resp.status_code == 409, resp.get_data(as_text=True)
    db.session.expire_all()
    person = _person(delivery_driver)
    assert person.current_location_lat == pytest.approx(41.35)
    assert person.current_location_lng == pytest.approx(69.31)
    assert person.location_accuracy_m == 15.0
    assert person.last_location_update == previous_update


def test_admin_force_close_does_not_move_the_driver(
    client, db, driver_auth_headers, admin_auth_headers, delivery_driver, driver_with_location, warehouse_coords
):
    """An admin closing an abandoned session is sitting at a desk. The driver is
    wherever they actually are — usually not the depot."""
    opened = client.post(OPEN_URL, json={"bottles_loaded": 20}, headers=driver_auth_headers)
    session_id = opened.get_json()["data"]["id"]
    _share_pin(client, driver_auth_headers, 41.35, 69.31, accuracy=15.0)
    db.session.expire_all()
    previous_update = _person(delivery_driver).last_location_update

    resp = client.post(
        f"/api/v1/admin/bottles/sessions/{session_id}/force-close",
        json={"reason": "abandoned overnight"},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    db.session.expire_all()
    person = _person(delivery_driver)
    assert person.current_location_lat == pytest.approx(41.35)
    assert person.current_location_lng == pytest.approx(69.31)
    assert person.last_location_update == previous_update


def test_session_opened_by_another_actor_does_not_move_the_driver(
    app, db, delivery_driver, driver_with_location, admin_user, warehouse_coords
):
    """`actor_user_id` exists so a non-driver can act on a driver's session. Such
    a caller knows nothing about where the driver is standing."""
    from business_app.services.bottle_tracking_service import BottleTrackingService

    before_lat = _person(delivery_driver).current_location_lat
    before_update = _person(delivery_driver).last_location_update

    BottleTrackingService().open_bottle_session(
        delivery_driver.id, 20, actor_user_id=admin_user.id
    )

    db.session.expire_all()
    person = _person(delivery_driver)
    assert person.current_location_lat == pytest.approx(before_lat)
    assert person.last_location_update == before_update


def test_driver_with_no_delivery_person_row_can_still_load_bottles(
    client, db, driver_auth_headers, delivery_driver, warehouse_coords
):
    """The anchor is a side effect. A missing profile must not fail the action
    the driver actually asked for."""
    assert _person(delivery_driver) is None

    resp = client.post(OPEN_URL, json={"bottles_loaded": 20}, headers=driver_auth_headers)

    assert resp.status_code == 201, resp.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# The payoff: route optimization stops begging for a pin
# --------------------------------------------------------------------------- #


def test_optimize_route_stops_demanding_a_pin_after_loading_bottles(
    client, db, driver_auth_headers, delivery_driver, driver_person_without_location, warehouse_coords
):
    """A driver who has never shared a position gets 412 LOCATION_REQUIRED.
    Loading bottles is a position, so the next tap must go through."""
    before = client.post("/api/v1/staff/delivery/optimize-route", headers=driver_auth_headers)
    assert before.status_code == 412
    assert before.get_json()["error_code"] == "LOCATION_REQUIRED"

    client.post(OPEN_URL, json={"bottles_loaded": 20}, headers=driver_auth_headers)

    after = client.post("/api/v1/staff/delivery/optimize-route", headers=driver_auth_headers)
    assert after.status_code == 200, after.get_data(as_text=True)


def test_warehouse_anchor_is_fresh_enough_to_start_a_route_from(
    app, client, db, driver_auth_headers, delivery_driver, driver_person_without_location, warehouse_coords
):
    """`_resolve_start_point` must pick the anchor as the driver's own position,
    not fall all the way through to its last-resort warehouse branch."""
    from business_app.services.route_optimization_service import RouteOptimizationService

    client.post(OPEN_URL, json={"bottles_loaded": 20}, headers=driver_auth_headers)

    db.session.expire_all()
    service = RouteOptimizationService()
    point, source = service._resolve_start_point(delivery_driver.id, [])

    assert point == pytest.approx((WAREHOUSE_LAT, WAREHOUSE_LNG))
    assert source == "driver_live"
    assert service.location_status(delivery_driver.id) == "fresh"
