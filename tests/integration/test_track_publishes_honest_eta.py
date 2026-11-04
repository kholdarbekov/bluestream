"""Track publishes the honest arrival fields, and the formula leaves the customer's payloads (honest-ETA Phase 1).

Spec: docs/superpowers/specs/2026-11-03-honest-capacity-aware-eta-design.md §4.8 (what the customer sees) and §7
("Contract": the bot and the website render only the published fields; a test fails if `estimated_time_remaining` or
the formula comes back).

`GET /api/v1/orders/<id>/track` carries five top-level keys, finished by `DeliveryEtaService.customer_eta`:
`eta_stage`, `eta_by`, `eta_by_time`, `eta_minutes_left` and `eta_attribution`. The hours-and-minutes countdown to
`deliveries.estimated_delivery_time` (`estimated_time_remaining`) is gone, and so is that formula ETA from the
customer's delivery payloads.

Driven through the routes the bot and the web call, with the customer's own token. The rows are the ones production
leaves, built with the helpers of tests/unit/test_delivery_eta_service.py: the delivery, the driver's real `in_transit`
tap in its status history, and that tap's shown Google promise. Every delivery also carries a formula ETA two hours
out, the way the dispatcher stamps it, so a test that must not see it can fail when it leaks.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from shared.constants import DISPLAY_TIMEZONE
from shared.enums import DeliveryStatus, OrderStatus
from tests.unit.test_delivery_eta_service import _moved, _order_on_its_way, _promise_of, _tap

pytestmark = pytest.mark.integration

ORDERS_URL = "/api/v1/orders/"
ORDER_URL = "/api/v1/orders/{order_id}"
TRACK_URL = "/api/v1/orders/{order_id}/track"

# The literals the bot and the web pages spell out (shared/constants.py), so a changed constant fails here.
ON_THE_WAY = "on_the_way"
ARRIVED = "arrived"
GOOGLE_MAPS = "google_maps"

ETA_KEYS = ("eta_stage", "eta_by", "eta_by_time", "eta_minutes_left", "eta_attribution")
NOTHING_PROMISED = dict.fromkeys(ETA_KEYS)
TASHKENT = ZoneInfo(DISPLAY_TIMEZONE)


def _get(client, url, headers):
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]


def _track(client, headers, delivery):
    return _get(client, TRACK_URL.format(order_id=delivery.order_id), headers)


def _eta(track):
    """The five arrival keys of a Track body. A key the payload does not carry raises."""
    return {key: track[key] for key in ETA_KEYS}


def _keys_in(body):
    """Every key anywhere in a JSON body."""
    if isinstance(body, dict):
        for key, value in body.items():
            yield key
            yield from _keys_in(value)
    elif isinstance(body, list):
        for item in body:
            yield from _keys_in(item)


def _delivery(db, customer, driver, status=DeliveryStatus.IN_TRANSIT, order_status=OrderStatus.OUT_FOR_DELIVERY):
    """The customer's delivery in `status`, with the formula ETA the dispatcher stamps on it, two hours out."""
    delivery = _order_on_its_way(db, customer, driver, delivery_status=status, order_status=order_status)
    delivery.estimated_delivery_time = datetime.now(UTC) + timedelta(hours=2)
    db.session.commit()
    return delivery


def test_on_the_way_publishes_minutes_the_by_time_and_attribution(
    client, db, auth_headers, sample_user, delivery_driver
):
    now = datetime.now(UTC).replace(microsecond=0)
    delivery = _delivery(db, sample_user, delivery_driver)
    by = now + timedelta(minutes=25)
    _promise_of(db, _tap(db, delivery, delivery_driver, at=now - timedelta(minutes=4)), window_end=by)

    track = _track(client, auth_headers, delivery)

    assert track["eta_stage"] == ON_THE_WAY
    # 25 minutes to the second. A slow run may let one minute pass before the read.
    assert track["eta_minutes_left"] in (24, 25)
    assert track["eta_by_time"] == by.astimezone(TASHKENT).strftime("%H:%M")
    # The instant is published on the customer's clock (+05:00), and is the promised one.
    assert track["eta_by"].endswith("+05:00")
    assert datetime.fromisoformat(track["eta_by"]) == by
    assert track["eta_attribution"] == GOOGLE_MAPS


def test_arrived_publishes_the_arrived_stage(client, db, auth_headers, sample_user, delivery_driver):
    """Arrived is the stage alone: the promise of the trip that has ended, still in the future, is not published."""
    now = datetime.now(UTC).replace(microsecond=0)
    delivery = _delivery(db, sample_user, delivery_driver, DeliveryStatus.ARRIVED)
    tap = _tap(db, delivery, delivery_driver, at=now - timedelta(minutes=20))
    _promise_of(db, tap, window_end=now + timedelta(minutes=9))
    _moved(db, delivery, DeliveryStatus.IN_TRANSIT, DeliveryStatus.ARRIVED, at=now, by=delivery_driver)

    track = _track(client, auth_headers, delivery)

    assert _eta(track) == {**NOTHING_PROMISED, "eta_stage": ARRIVED}


@pytest.mark.parametrize(
    "delivery_status, order_status",
    [
        pytest.param(DeliveryStatus.ASSIGNED, OrderStatus.CONFIRMED, id="assigned"),
        pytest.param(DeliveryStatus.PICKED_UP, OrderStatus.OUT_FOR_DELIVERY, id="picked-up"),
        pytest.param(DeliveryStatus.SCHEDULED, OrderStatus.CONFIRMED, id="scheduled"),
    ],
)
def test_nothing_is_promised_before_the_driver_sets_off(
    client, db, auth_headers, sample_user, delivery_driver, delivery_status, order_status
):
    """An earlier trip's promise is on record, as after a failed attempt that was sent out again. It must not stand
    in for a trip that has not begun."""
    now = datetime.now(UTC).replace(microsecond=0)
    delivery = _delivery(db, sample_user, delivery_driver, delivery_status, order_status)
    earlier_trip = _tap(db, delivery, delivery_driver, at=now - timedelta(minutes=20))
    _promise_of(db, earlier_trip, window_end=now + timedelta(minutes=9))

    track = _track(client, auth_headers, delivery)

    assert _eta(track) == NOTHING_PROMISED


def test_a_redispatched_delivery_never_shows_its_earlier_trips_minutes(
    client, db, auth_headers, sample_user, delivery_driver
):
    """Out with a promise, failed, out again. Until the new tap's own promise is recorded Track says "on the way" and
    nothing else, though the old trip's promise is still in the future. Then the new tap's promise is the one shown."""
    now = datetime.now(UTC).replace(microsecond=0)
    delivery = _delivery(db, sample_user, delivery_driver)
    first_trip = _tap(db, delivery, delivery_driver, at=now - timedelta(minutes=25))
    _promise_of(db, first_trip, window_end=now + timedelta(minutes=4))
    for old, new, minutes_ago in (
        (DeliveryStatus.IN_TRANSIT, DeliveryStatus.FAILED, 20),
        (DeliveryStatus.FAILED, DeliveryStatus.ASSIGNED, 15),
        (DeliveryStatus.ASSIGNED, DeliveryStatus.PICKED_UP, 12),
    ):
        _moved(db, delivery, old, new, at=now - timedelta(minutes=minutes_ago), by=delivery_driver)
    second_trip = _tap(db, delivery, delivery_driver, at=now - timedelta(minutes=10))

    track = _track(client, auth_headers, delivery)

    assert _eta(track) == {**NOTHING_PROMISED, "eta_stage": ON_THE_WAY}

    _promise_of(db, second_trip, window_end=now + timedelta(minutes=19))

    track = _track(client, auth_headers, delivery)

    assert (track["eta_stage"], track["eta_attribution"]) == (ON_THE_WAY, GOOGLE_MAPS)
    assert track["eta_minutes_left"] in (18, 19)
    assert track["eta_by_time"] == (now + timedelta(minutes=19)).astimezone(TASHKENT).strftime("%H:%M")


def test_the_countdown_field_is_gone(client, db, auth_headers, sample_user, delivery_driver):
    """The delivery's formula ETA is two hours out, which the old field counted down to."""
    now = datetime.now(UTC).replace(microsecond=0)
    delivery = _delivery(db, sample_user, delivery_driver)
    _promise_of(
        db, _tap(db, delivery, delivery_driver, at=now - timedelta(minutes=4)), window_end=now + timedelta(minutes=25)
    )

    track = _track(client, auth_headers, delivery)

    assert "estimated_time_remaining" not in track


def test_customers_never_receive_the_formula_eta(client, db, auth_headers, sample_user, delivery_driver):
    delivery = _delivery(db, sample_user, delivery_driver)
    assert delivery.estimated_delivery_time is not None

    track = _track(client, auth_headers, delivery)
    detail = _get(client, ORDER_URL.format(order_id=delivery.order_id), auth_headers)
    listed = _get(client, ORDERS_URL, auth_headers)

    # The delivery is published on each read, so its missing key is not a missing delivery.
    assert "error" not in detail["order"], f"the degraded fallback answered: {detail['order']}"
    for published in (track["delivery"], detail["delivery"], detail["order"]["delivery_info"]):
        assert published["tracking_number"] == delivery.tracking_number
        assert "estimated_delivery_time" not in published
    # Nor is it anywhere else on the three reads the bot and the web make of an order.
    for body in (track, detail, listed):
        assert "estimated_delivery_time" not in set(_keys_in(body))
