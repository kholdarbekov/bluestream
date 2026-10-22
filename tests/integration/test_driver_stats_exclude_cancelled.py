"""A driver's stats leave out cancelled deliveries (owner, 2026-10-22).

A cancel is the customer's or an admin's decision, not something the driver did, so a CANCELLED
delivery that still names the driver is not one of their deliveries in the stats. The rule reads
the DELIVERY's status, and only CANCELLED: a delivery that FAILED and whose order was cancelled
afterwards stays FAILED, and still counts, because the failed attempt was the driver's real
outcome.

Both screens that show these numbers read one backend answer, `StaffService.get_delivery_stats`,
and are driven here through their real routes:
- the staff bot's "My stats": `GET /api/v1/staff/delivery/stats`, bare on first open and then with
  `?period=day|week|month` from the period buttons (staff_bot/handlers/delivery/history.py);
- the admin driver card: `GET /api/v1/admin/staff/delivery-persons/<id>` (DeliveryPersons.js),
  which shows the month.
"""

from datetime import timedelta

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.utils.local_windows import local_date
from shared.enums import DeliveryStatus
from tests.integration.test_customer_cancel_order import (
    CANCEL_URL,
    DS,
    OS,
    PS,
    Case,
    _headers,
    _on_duty,
    _seed,
)

DRIVER_STATS_URL = "/api/v1/staff/delivery/stats"
ADMIN_DRIVER_URL = "/api/v1/admin/staff/delivery-persons/{person_id}"
ADMIN_STATUS_URL = "/api/v1/admin/orders/{order_id}/status"
# What "My stats" sends: nothing on first open, then one of the three period buttons.
BOT_STATS_QUERIES = ("", "?period=day", "?period=week", "?period=month")
STATS_KEYS = ("total_deliveries", "delivered", "failed", "success_rate")


def _counted(stats):
    return {key: stats[key] for key in STATS_KEYS}


def _bot_stats(client, app, driver):
    headers = _headers(app, driver)
    answers = {}
    for query in BOT_STATS_QUERIES:
        response = client.get(DRIVER_STATS_URL + query, headers=headers)
        assert response.status_code == 200, response.get_json()
        answers[query] = _counted(response.get_json()["data"])
    return answers


def _admin_card_stats(client, headers, person_id):
    response = client.get(ADMIN_DRIVER_URL.format(person_id=person_id), headers=headers)
    assert response.status_code == 200, response.get_json()
    return _counted(response.get_json()["data"]["delivery_person"]["stats"])


def _every_period(expected):
    return {query: expected for query in BOT_STATS_QUERIES}


@pytest.fixture
def driver_day(db, sample_user, delivery_driver):
    """One driver's day: a delivered stop, a failed one, and one accepted and not yet picked up.
    Returns `(profile, {name: (order, delivery)})`."""
    _on_duty(db, delivery_driver)
    cases = {
        "delivered": Case("stats-delivered", OS.DELIVERED, True, PS.COMPLETED, DS.DELIVERED, None),
        "failed": Case("stats-failed", OS.CONFIRMED, False, PS.PENDING, DS.FAILED, None),
        "accepted": Case("stats-accepted", OS.CONFIRMED, False, PS.PENDING, DS.ASSIGNED, None),
    }
    seeded = {name: _seed(db, sample_user, delivery_driver, case) for name, case in cases.items()}
    return DeliveryPerson.query.filter_by(user_id=delivery_driver.id).one(), seeded


BEFORE_THE_CANCEL = {"total_deliveries": 3, "delivered": 1, "failed": 1, "success_rate": 33.3}
AFTER_THE_CANCEL = {"total_deliveries": 2, "delivered": 1, "failed": 1, "success_rate": 50.0}


@pytest.mark.integration
def test_a_customer_cancel_leaves_the_drivers_stats(
    app, client, db, sample_user, delivery_driver, admin_claim_headers, driver_day
):
    profile, seeded = driver_day
    order, delivery = seeded["accepted"]
    assert _bot_stats(client, app, delivery_driver) == _every_period(BEFORE_THE_CANCEL)
    stored_counters = (profile.total_deliveries, profile.successful_deliveries)

    cancelled = client.post(CANCEL_URL.format(order_id=order.id), headers=_headers(app, sample_user))

    assert cancelled.status_code == 200, cancelled.get_json()
    db.session.expire_all()
    landed = db.session.get(Delivery, delivery.id)
    assert (landed.status, landed.delivery_person_id) == (DeliveryStatus.CANCELLED, delivery_driver.id)
    assert _bot_stats(client, app, delivery_driver) == _every_period(AFTER_THE_CANCEL)
    assert _admin_card_stats(client, admin_claim_headers, profile.id) == AFTER_THE_CANCEL
    # The stored counters never counted a cancel, and still do not.
    profile = db.session.get(DeliveryPerson, profile.id)
    assert (profile.total_deliveries, profile.successful_deliveries) == stored_counters


@pytest.mark.integration
def test_an_admin_cancel_leaves_the_drivers_stats(
    app, client, db, delivery_driver, admin_claim_headers, driver_day
):
    profile, seeded = driver_day
    order, _delivery = seeded["accepted"]
    assert _admin_card_stats(client, admin_claim_headers, profile.id) == BEFORE_THE_CANCEL

    # The Orders page's Cancel (CloseOrderDialog): `{status, reason}`.
    cancelled = client.put(
        ADMIN_STATUS_URL.format(order_id=order.id),
        json={"status": "cancelled", "reason": "Customer changed their mind"},
        headers=admin_claim_headers,
    )

    assert cancelled.status_code == 200, cancelled.get_json()
    assert _admin_card_stats(client, admin_claim_headers, profile.id) == AFTER_THE_CANCEL
    assert _bot_stats(client, app, delivery_driver) == _every_period(AFTER_THE_CANCEL)


@pytest.mark.integration
def test_a_failed_delivery_whose_order_is_cancelled_afterwards_still_counts_as_failed(
    app, client, db, delivery_driver, admin_claim_headers, driver_day
):
    """Cancelling the order is one of the two ways out of a failed delivery (F2). The cascade
    leaves the FAILED row as it is, and the stats keep it as the driver's failure."""
    profile, seeded = driver_day
    order, delivery = seeded["failed"]

    cancelled = client.put(
        ADMIN_STATUS_URL.format(order_id=order.id),
        json={"status": "cancelled", "reason": "Customer no longer needs it"},
        headers=admin_claim_headers,
    )

    assert cancelled.status_code == 200, cancelled.get_json()
    db.session.expire_all()
    assert db.session.get(Delivery, delivery.id).status is DeliveryStatus.FAILED
    assert _bot_stats(client, app, delivery_driver) == _every_period(BEFORE_THE_CANCEL)
    assert _admin_card_stats(client, admin_claim_headers, profile.id) == BEFORE_THE_CANCEL


@pytest.mark.integration
def test_a_legacy_cancelled_delivery_naming_the_driver_is_left_out(
    app, client, db, sample_user, delivery_driver, admin_claim_headers, driver_day
):
    """Before drivers lost their own cancel (F4), a driver-cancelled delivery kept its driver.
    Such a row, seeded as it was left, is no more the driver's than a customer's cancel is."""
    profile, _seeded = driver_day
    _order, legacy = _seed(
        db, sample_user, delivery_driver, Case("stats-legacy", OS.CANCELLED, False, PS.CANCELLED, DS.CANCELLED, None)
    )
    assert legacy.delivery_person_id == delivery_driver.id

    assert _bot_stats(client, app, delivery_driver) == _every_period(BEFORE_THE_CANCEL)
    assert _admin_card_stats(client, admin_claim_headers, profile.id) == BEFORE_THE_CANCEL


ANALYTICS_DELIVERY_URL = "/api/v1/analytics/delivery"


@pytest.mark.integration
def test_the_analytics_driver_performance_leaves_cancelled_deliveries_out(
    client, db, sample_user, delivery_driver, second_delivery_driver, admin_claim_headers
):
    """`GET /analytics/delivery` with the query the Analytics page sends on first open: its 30-day
    range as two local dates (`Analytics.js`, `getTimeframeDateRange('30d')` formatted
    `YYYY-MM-DD`) plus the `period` that `buildAnalyticsParams` derives from the timeframe. The
    backend answers that as a custom range. Its `driver_performance` is built per driver from
    the deliveries in the range: the first driver has one delivered and one cancelled delivery,
    the second only a cancelled one. Only the delivered one counts, and the second driver is no
    driver at all in these figures."""
    seeds = (
        (Case("analytics-delivered", OS.DELIVERED, True, PS.COMPLETED, DS.DELIVERED, None), delivery_driver),
        (Case("analytics-cancelled", OS.CANCELLED, False, PS.CANCELLED, DS.CANCELLED, None), delivery_driver),
        (Case("analytics-only-cancelled", OS.CANCELLED, False, PS.CANCELLED, DS.CANCELLED, None),
         second_delivery_driver),
    )
    for case, driver in seeds:
        _seed(db, sample_user, driver, case)

    today = local_date()
    response = client.get(
        ANALYTICS_DELIVERY_URL,
        query_string={
            "period": "month",
            "start_date": (today - timedelta(days=30)).isoformat(),
            "end_date": today.isoformat(),
        },
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["period"] == "custom"
    performance = response.get_json()["delivery_analytics"]["driver_performance"]
    assert (performance["total_drivers"], performance["average_deliveries_per_driver"]) == (1, 1.0)
    [top] = performance["top_performers"]
    assert (top["driver_id"], top["total_deliveries"], top["successful_deliveries"], top["failed_deliveries"]) == (
        delivery_driver.id, 1, 1, 0,
    )
    assert top["success_rate"] == 100.0
