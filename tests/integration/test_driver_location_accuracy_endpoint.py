"""Spec §5.3 + §4.2: accuracy gating on the driver-location endpoint, and the
optimize endpoint refusing a stale fix rather than solving from it.

Driven through HTTP because the bot's behaviour keys on the response
`error_code`, not on the service's exception type."""

import json as json_module
from datetime import datetime, timedelta, timezone

import pytest

from business_app.models.delivery import DeliveryPerson

pytestmark = pytest.mark.integration


def _person(delivery_driver):
    return DeliveryPerson.query.filter_by(user_id=delivery_driver.id).first()


def test_location_with_good_accuracy_is_stored(
    client, db, driver_auth_headers, delivery_driver, driver_with_location
):
    resp = client.post(
        "/api/v1/staff/delivery/me/location",
        json={"latitude": 41.31, "longitude": 69.28, "horizontal_accuracy": 25.0},
        headers=driver_auth_headers,
    )

    assert resp.status_code == 200
    person = _person(delivery_driver)
    assert person.location_accuracy_m == 25.0
    assert person.current_location_lat == pytest.approx(41.31)


def test_location_without_accuracy_is_accepted(
    client, db, driver_auth_headers, delivery_driver, driver_with_location
):
    """Not every Telegram client reports horizontal_accuracy; a driver must not
    be blocked by their client version."""
    resp = client.post(
        "/api/v1/staff/delivery/me/location",
        json={"latitude": 41.31, "longitude": 69.28},
        headers=driver_auth_headers,
    )

    assert resp.status_code == 200
    person = _person(delivery_driver)
    assert person.location_accuracy_m is None
    assert person.current_location_lat == pytest.approx(41.31)


def test_coarse_location_is_refused_and_previous_fix_survives(
    client, db, driver_auth_headers, delivery_driver, driver_with_location
):
    """The whole point: a 900m-uncertain fix must not overwrite a precise one.
    Routing from a known-bad origin is worse than routing from an older one."""
    client.post(
        "/api/v1/staff/delivery/me/location",
        json={"latitude": 41.31, "longitude": 69.28, "horizontal_accuracy": 20.0},
        headers=driver_auth_headers,
    )
    db.session.expire_all()
    previous_update = _person(delivery_driver).last_location_update

    resp = client.post(
        "/api/v1/staff/delivery/me/location",
        json={"latitude": 41.90, "longitude": 69.90, "horizontal_accuracy": 900.0},
        headers=driver_auth_headers,
    )

    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "LOCATION_TOO_COARSE"
    db.session.expire_all()
    person = _person(delivery_driver)
    assert person.current_location_lat == pytest.approx(41.31)
    assert person.current_location_lng == pytest.approx(69.28)
    assert person.location_accuracy_m == 20.0
    assert person.last_location_update == previous_update


def test_nan_accuracy_is_refused_and_previous_fix_survives(
    client, db, driver_auth_headers, delivery_driver, driver_with_location
):
    """NaN is a broken measurement, not an absent one: `nan < 0` and
    `nan > max_accuracy` are both False in Python, so without an explicit
    `math.isfinite` check a NaN reading would slip past both the negative
    and coarse-fix guards and get persisted — and later re-serialized as the
    non-standard `NaN` JSON token. Posted as a raw body (rather than the
    test client's `json=` kwarg) to exercise the real parse path: Python's
    `json.dumps`/`json.loads` accept a bare `NaN` token by default, so a
    real client can send exactly this."""
    client.post(
        "/api/v1/staff/delivery/me/location",
        json={"latitude": 41.31, "longitude": 69.28, "horizontal_accuracy": 20.0},
        headers=driver_auth_headers,
    )
    db.session.expire_all()
    previous_update = _person(delivery_driver).last_location_update

    body = json_module.dumps({"latitude": 41.90, "longitude": 69.90, "horizontal_accuracy": float("nan")})
    resp = client.post(
        "/api/v1/staff/delivery/me/location",
        data=body,
        headers=driver_auth_headers,
    )

    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "STAFF_INVALID_ACCURACY"
    db.session.expire_all()
    person = _person(delivery_driver)
    assert person.current_location_lat == pytest.approx(41.31)
    assert person.current_location_lng == pytest.approx(69.28)
    assert person.location_accuracy_m == 20.0
    assert person.last_location_update == previous_update


def test_infinite_accuracy_is_refused_and_previous_fix_survives(
    client, db, driver_auth_headers, delivery_driver, driver_with_location
):
    """+Infinity would also be caught by the '> max_accuracy' coarse-fix
    guard, but the finite check refuses it up front with the same
    STAFF_INVALID_ACCURACY code as NaN and the negative case — one answer
    to 'this accuracy value is unusable', not two different error codes for
    two flavors of unusable."""
    client.post(
        "/api/v1/staff/delivery/me/location",
        json={"latitude": 41.31, "longitude": 69.28, "horizontal_accuracy": 20.0},
        headers=driver_auth_headers,
    )
    db.session.expire_all()
    previous_update = _person(delivery_driver).last_location_update

    body = json_module.dumps({"latitude": 41.90, "longitude": 69.90, "horizontal_accuracy": float("inf")})
    resp = client.post(
        "/api/v1/staff/delivery/me/location",
        data=body,
        headers=driver_auth_headers,
    )

    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "STAFF_INVALID_ACCURACY"
    db.session.expire_all()
    person = _person(delivery_driver)
    assert person.current_location_lat == pytest.approx(41.31)
    assert person.current_location_lng == pytest.approx(69.28)
    assert person.location_accuracy_m == 20.0
    assert person.last_location_update == previous_update


def test_optimize_refuses_a_stale_fix(
    client, db, driver_auth_headers, delivery_driver, driver_with_location
):
    """Spec §4.2: today only a MISSING fix is refused, so a three-hour-old
    position silently produces a route solved from where the driver used to be."""
    person = _person(delivery_driver)
    person.last_location_update = datetime.now(timezone.utc) - timedelta(hours=3)
    db.session.commit()

    resp = client.post("/api/v1/staff/delivery/optimize-route", headers=driver_auth_headers)

    assert resp.status_code == 412
    assert resp.get_json()["error_code"] == "LOCATION_REQUIRED"
