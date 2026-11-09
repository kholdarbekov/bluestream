"""The admin accuracy report: the go-live gate and the figures behind it (honest-ETA Phase 2, Task 9; spec §5, §9).

Driven through `GET /api/v1/admin/eta/accuracy?start_date=&end_date=`, the route the admin page calls, with a manager's
claim headers. Its inputs are the rows the real writers leave: orders and their deliveries, the status taps of drivers,
admins and the Orders screen (`eta_plan_builders.tap`), the engine's hidden S1 rows as the recorder writes them
(`engine_promise`), Phase 1's shown S2 promises and their hidden shadows (`shown_promise`, `shadow_promise`), and a
weekly fit (`add_fit`).

- The gate (spec §5, R12, R14). Each order's FIRST engine row is its promise, judged by its state when the report runs:
  dated, no window, cancelled, returned, closed by someone other than its driver, still open, or judged. A judged order
  is kept when its driver's own delivered tap came by that row's window end.
- The figures below the gate read the judged orders: buckets, window widths, the day figures, early arrivals, cars
  working, and the change messages the §4.9 rule would send. S2 reads the legs Phase 1's headroom learns from.

The report's clock is the route's one reading, pinned to `NOW` (15:00 in Tashkent on Wednesday 4 November 2026); every
row is written at an instant on the Tashkent wall clock (`at`), so no test depends on the time of day. The plan day
under test is `DAY`, Tuesday 3 November. The scenario's constants live in `eta_plan_builders` (`REPORT_*`), which the
contract test shares. A route that judges a date against the real clock (a reschedule) is given one relative to it.

The page's fixture, `tests/fixtures/admin_eta_accuracy/report.json`, is this module's report of `_every_figure`. The
admin UI's Vitest reads it (Task 10), and `tests/contract/test_eta_accuracy_payload_matches_admin_fixture.py` holds the
report to its keys. This module writes it when it is missing and fails when it is stale: delete it and re-run.
"""

import json
from datetime import datetime, timedelta
from typing import Optional

import pytest

from business_app.api import admin_eta
from business_app.middleware.timezone_middleware import TimezoneMiddleware
from business_app.models.delivery import DeliveryStatusHistory
from business_app.models.order import Order
from business_app.services.auth_service import AuthService
from business_app.services.delivery_eta_service import DeliveryEtaService
from business_app.services.eta_plan.report import EtaAccuracyReport
from business_app.tasks.order_tasks import release_due_scheduled_orders
from business_app.utils.local_windows import local_date
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType
from tests.integration.eta_plan_builders import REPORT_DAY as DAY
from tests.integration.eta_plan_builders import REPORT_FIT_MONDAY as FIT_MONDAY
from tests.integration.eta_plan_builders import REPORT_FIXTURE as FIXTURE
from tests.integration.eta_plan_builders import REPORT_MONITORING as MONITORING
from tests.integration.eta_plan_builders import REPORT_NOW as NOW
from tests.integration.eta_plan_builders import add_fit, bind, dated_order, engine_promise, make_driver, open_session
from tests.integration.eta_plan_builders import order_at_km, reassign
from tests.integration.eta_plan_builders import rebase_clock_fixture  # noqa: F401 -- the builders' fixture
from tests.integration.eta_plan_builders import report_day_at as at
from tests.integration.eta_plan_builders import schedule, shadow_promise, shown_promise, stale_estimate_promise, tap
from tests.integration.reorder_reminder_builders import claim_headers, make_customer

pytestmark = pytest.mark.integration

REPORT = "/api/v1/admin/eta/accuracy"
RESCHEDULE = "/api/v1/admin/orders/{}/schedule"  # the Reschedule modal's route (adminService.rescheduleOrder)
ADMIN_DELIVERY = "/api/v1/admin/deliveries/{}"  # the Delivery page's Update status (adminService.updateDelivery)

TODAY = DAY + timedelta(days=1)
ONE_DAY = {"start_date": DAY.isoformat(), "end_date": DAY.isoformat()}


@pytest.fixture(autouse=True)
def report_clock(monkeypatch):
    """The route reads the clock once; it reads `NOW`."""
    monkeypatch.setattr(admin_eta, "get_utc_now", lambda: NOW)


@pytest.fixture
def driver(db):
    return make_driver(db)


def report(client, headers, **params):
    response = client.get(REPORT, query_string=params, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]


def placed(db, driver, placed_at=None):
    """An order placed at `placed_at` (08:00 on DAY) and assigned to `driver`."""
    return order_at_km(db, 2.0, created_at=placed_at or at(8), driver=driver)


def promised(db, order, computed_at, start, end, **row):
    """An engine row promising `order` the window `start`..`end`, E half an hour into it."""
    return engine_promise(
        db, order.delivery, computed_at, expected_at=start + timedelta(minutes=30), window=(start, end), **row
    )


def delivered(db, order, when, **by):
    """`order` delivered at `when`: by its own driver, or with `by=` an admin, or None for the Orders screen."""
    return tap(db, order.delivery, DeliveryStatus.DELIVERED, when, **by)


def ended(db, order, order_status: OrderStatus, delivery_status: DeliveryStatus):
    """`order` cancelled or returned: the order's status and its delivery's."""
    order.status = order_status
    order.delivery.status = delivery_status
    db.session.commit()


def leg(
    db,
    driver,
    tapped_at: datetime,
    *,
    drive: Optional[float] = 23,
    by_minutes: float = 29,
    shadow: Optional[float] = 11,
    arrived_by=None,
):
    """One of Phase 1's legs: the driver's in_transit tap at `tapped_at`, its shown promise of Google's 18 minutes by
    the tap + `by_minutes`, our engine's `shadow` minutes (None: no shadow row), and the arrival `drive` minutes after
    the tap (None: not yet), tapped by the driver or by the User `arrived_by`. Returns the order."""
    order = order_at_km(db, 2.0, created_at=tapped_at - timedelta(hours=1), driver=driver)
    tap(db, order.delivery, DeliveryStatus.PICKED_UP, tapped_at - timedelta(minutes=5))
    tapped = tap(db, order.delivery, DeliveryStatus.IN_TRANSIT, tapped_at)
    shown_promise(db, tapped, google_minutes=18, window_end=tapped_at + timedelta(minutes=by_minutes))
    if shadow is not None:
        shadow_promise(db, tapped, minutes=shadow)
    if drive is not None:
        by = {"by": arrived_by} if arrived_by is not None else {}
        tap(db, order.delivery, DeliveryStatus.ARRIVED, tapped_at + timedelta(minutes=drive), **by)
    return order


def stale_leg(db, driver, tapped_at: datetime, *, engine: float, by_minutes: float, drive: float):
    """One of the legs whose driver's position was too old for Google at the tap (`stale_estimate`): our engine's
    `engine` minutes (the hidden shadow), the shown promise by the tap + `by_minutes` (the engine's minutes and an
    allowance), and the driver's own arrival `drive` minutes after the tap. Returns the order."""
    order = order_at_km(db, 2.0, created_at=tapped_at - timedelta(hours=1), driver=driver)
    tap(db, order.delivery, DeliveryStatus.PICKED_UP, tapped_at - timedelta(minutes=5))
    tapped = tap(db, order.delivery, DeliveryStatus.IN_TRANSIT, tapped_at)
    stale_estimate_promise(db, tapped, window_end=tapped_at + timedelta(minutes=by_minutes))
    shadow_promise(db, tapped, minutes=engine)
    tap(db, order.delivery, DeliveryStatus.ARRIVED, tapped_at + timedelta(minutes=drive))
    return order


def _one_order_per_category(db, driver):
    """One order in each gate category, and two judged ones: one kept, one missed. All placed on DAY."""
    dated_order(db, DAY + timedelta(days=2), created_at=at(9))  # held for its day: no delivery row yet

    no_window = placed(db, driver)
    engine_promise(db, no_window.delivery, at(9))

    cancelled = placed(db, driver)
    promised(db, cancelled, at(9), at(10), at(12))
    ended(db, cancelled, OrderStatus.CANCELLED, DeliveryStatus.CANCELLED)

    returned = placed(db, driver)
    promised(db, returned, at(9), at(10), at(12))
    ended(db, returned, OrderStatus.RETURNED, DeliveryStatus.RETURNED)

    closed_on_the_orders_screen = placed(db, driver)
    promised(db, closed_on_the_orders_screen, at(9), at(10), at(12))
    delivered(db, closed_on_the_orders_screen, at(11), by=None)

    still_open = placed(db, driver, placed_at=at(19))
    promised(db, still_open, at(20), at(16, day=TODAY), at(18, day=TODAY))

    kept = placed(db, driver)
    promised(db, kept, at(9), at(10), at(12))
    delivered(db, kept, at(11))

    missed = placed(db, driver)
    promised(db, missed, at(9), at(10), at(12))
    return kept, missed


# --------------------------------------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------------------------------------


def test_the_gate_counts_each_orders_first_promise_only(client, db, manager_claim_headers, driver):
    """Promised 10:00-12:00, then re-promised 13:00-15:00 and delivered at 14:00: a miss, judged by the first promise.
    An order first promised the evening before the range is not in the range's sample, whatever its later rows say."""
    judged = placed(db, driver)
    promised(db, judged, at(9), at(10), at(12))
    promised(db, judged, at(11), at(13), at(15))
    delivered(db, judged, at(14))
    earlier = placed(db, driver, placed_at=at(20, day=DAY - timedelta(days=1)))
    promised(db, earlier, at(21, day=DAY - timedelta(days=1)), at(10), at(12))
    promised(db, earlier, at(9), at(13), at(15))
    delivered(db, earlier, at(14))

    gate = report(client, manager_claim_headers, **ONE_DAY)["gate"]

    assert (gate["sample"], gate["n"], gate["kept"]) == (1, 1, 0)
    assert gate["kept_share"] == pytest.approx(0.0)


def test_an_unconfirmed_first_promise_is_excluded_not_replaced(client, db, manager_claim_headers, driver):
    order = placed(db, driver)
    engine_promise(db, order.delivery, at(9))  # no car could take it: no day, no time, no window
    promised(db, order, at(10), at(11), at(13))
    delivered(db, order, at(12))

    gate = report(client, manager_claim_headers, **ONE_DAY)["gate"]

    assert gate["excluded"]["no_window"] == 1
    assert (gate["sample"], gate["n"], gate["kept_share"]) == (1, 0, None)


def test_every_order_lands_in_exactly_one_gate_category(client, db, manager_claim_headers, driver):
    _one_order_per_category(db, driver)

    gate = report(client, manager_claim_headers, **ONE_DAY)["gate"]

    assert gate["excluded"] == {
        "dated": 1,
        "no_window": 1,
        "cancelled": 1,
        "returned": 1,
        "non_driver_close": 1,
        "open": 1,
    }
    assert (gate["n"], gate["kept"]) == (2, 1)
    assert gate["kept_share"] == pytest.approx(0.5)
    assert gate["sample"] == 8 == gate["n"] + sum(gate["excluded"].values())


def test_a_cancelled_or_returned_order_is_excluded_and_an_overdue_open_one_is_a_miss(
    client, db, manager_claim_headers, driver
):
    """Cancelled and returned orders are not judged. An order still undelivered past its window end is a miss; one
    whose window has not ended yet is open."""
    for order_status, delivery_status in (
        (OrderStatus.CANCELLED, DeliveryStatus.CANCELLED),
        (OrderStatus.RETURNED, DeliveryStatus.RETURNED),
    ):
        order = placed(db, driver)
        promised(db, order, at(9), at(10), at(12))
        ended(db, order, order_status, delivery_status)
    overdue = placed(db, driver)
    promised(db, overdue, at(9), at(10), at(12))
    tap(db, overdue.delivery, DeliveryStatus.PICKED_UP, at(10))
    not_yet = placed(db, driver, placed_at=at(19))
    promised(db, not_yet, at(20), at(15, day=TODAY), at(17, day=TODAY))  # ends two hours after NOW

    gate = report(client, manager_claim_headers, **ONE_DAY)["gate"]

    assert (gate["excluded"]["cancelled"], gate["excluded"]["returned"], gate["excluded"]["open"]) == (1, 1, 1)
    assert (gate["n"], gate["kept"]) == (1, 0)
    assert gate["kept_share"] == pytest.approx(0.0)


def test_an_early_delivery_is_kept_and_a_late_one_is_not(client, db, manager_claim_headers, driver):
    """Promised 12:00-14:00: delivered at 11:00 (early) and at 14:00 sharp are kept; at 14:01 is not."""
    for delivered_at in (at(11), at(14), at(14, 1)):
        order = placed(db, driver)
        promised(db, order, at(9), at(12), at(14))
        delivered(db, order, delivered_at)

    gate = report(client, manager_claim_headers, **ONE_DAY)["gate"]

    assert (gate["n"], gate["kept"]) == (3, 2)
    assert gate["kept_share"] == pytest.approx(2 / 3)


@pytest.mark.parametrize(
    "late, overall, before_eleven, passes",
    [
        pytest.param(None, 1.0, 1.0, True, id="both-kept"),
        pytest.param("11:00", 0.5, 1.0, False, id="the-11:00-order-late"),
        pytest.param("10:59", 0.5, 0.0, False, id="the-10:59-order-late"),
    ],
)
def test_orders_placed_before_eleven_are_reported_separately(
    client, db, manager_claim_headers, driver, late, overall, before_eleven, passes
):
    """Two orders, placed at 10:59 and at 11:00 local, promised 12:00-14:00. The gate passes only when both shares
    reach 90%."""
    for placed_at, label in ((at(10, 59), "10:59"), (at(11), "11:00")):
        order = placed(db, driver, placed_at=placed_at)
        promised(db, order, at(11, 5), at(12), at(14))
        delivered(db, order, at(15) if label == late else at(13))

    gate = report(client, manager_claim_headers, **ONE_DAY)["gate"]

    assert (gate["n"], gate["before_eleven"]["n"], gate["passes"]) == (2, 1, passes)
    assert (gate["kept_share"], gate["before_eleven"]["kept_share"]) == pytest.approx((overall, before_eleven))
    assert gate["target"] == pytest.approx(0.9)


def test_the_gate_fails_when_only_the_morning_orders_miss(client, db, manager_claim_headers, driver):
    """Ten orders placed at noon, all kept, and one placed at 10:00 and late: 10 of 11 overall (91%) is not enough
    when the morning's share is 0%."""
    for _ in range(10):
        order = placed(db, driver, placed_at=at(12))
        promised(db, order, at(12, 5), at(13), at(15))
        delivered(db, order, at(14))
    morning = placed(db, driver, placed_at=at(10))
    promised(db, morning, at(10, 5), at(11), at(13))
    delivered(db, morning, at(14))

    gate = report(client, manager_claim_headers, **ONE_DAY)["gate"]

    assert gate["kept_share"] == pytest.approx(10 / 11)
    assert gate["before_eleven"]["kept_share"] == pytest.approx(0.0)
    assert gate["passes"] is False


def test_an_orders_screen_or_admin_close_is_excluded_and_counted(
    client, db, manager_claim_headers, admin_claim_headers, driver, admin_user
):
    """Closed from the Orders screen (a delivered row with no actor), and by an admin from the Delivery page's Update
    status, with the body that form sends, on a stop its driver brought to the door: neither time is the driver's
    handover. Only the driver's own tap is judged."""
    on_the_orders_screen = placed(db, driver)
    promised(db, on_the_orders_screen, at(9), at(10), at(12))
    delivered(db, on_the_orders_screen, at(11), by=None)
    on_the_delivery_page = placed(db, driver)
    promised(db, on_the_delivery_page, at(9), at(10), at(12))
    bind(db, open_session(db, driver, at(8)), on_the_delivery_page)
    for status, when in (
        (DeliveryStatus.PICKED_UP, at(10)),
        (DeliveryStatus.IN_TRANSIT, at(10, 15)),
        (DeliveryStatus.ARRIVED, at(10, 40)),
    ):
        tap(db, on_the_delivery_page.delivery, status, when)
    closed = client.put(
        ADMIN_DELIVERY.format(on_the_delivery_page.delivery.id),
        json={"status": "delivered", "notes": ""},
        headers=admin_claim_headers,
    )
    assert closed.status_code == 200, closed.get_data(as_text=True)
    [close] = DeliveryStatusHistory.query.filter_by(
        delivery_id=on_the_delivery_page.delivery.id, new_status=DeliveryStatus.DELIVERED
    ).all()
    assert close.changed_by == admin_user.id
    own = placed(db, driver)
    promised(db, own, at(9), at(10), at(12))
    delivered(db, own, at(11))

    gate = report(client, manager_claim_headers, **ONE_DAY)["gate"]

    assert gate["excluded"]["non_driver_close"] == 2
    assert (gate["sample"], gate["n"], gate["kept"]) == (3, 1, 1)


def test_a_date_only_order_is_counted_as_dated(rebase_clock, client, db, manager_claim_headers, driver):
    """Two orders placed on DAY for a later day, whose first promise was that date on the success screen. One is
    released by the real sweep on its day, when its delivery row first appears; its first engine row there has no
    window, and it is still `dated`, not `no_window`. The other is still held, with no delivery row. An order dated for
    the day it was placed is an ordinary one, and a dated order placed the day before the range is not in its
    sample."""
    released = dated_order(db, TODAY, created_at=at(10))
    assert release_due_scheduled_orders()["released"] == 1  # its day is behind the real clock, so it is due
    rebase_clock(at(6, day=TODAY))  # the sweep ran at 06:00 on its day
    db.session.expire_all()
    engine_promise(db, db.session.get(Order, released.id).delivery, at(6, 5, day=TODAY))
    dated_order(db, DAY + timedelta(days=3), created_at=at(10))
    same_day = placed(db, driver, placed_at=at(10))
    schedule(db, same_day, day=DAY)
    promised(db, same_day, at(10, 5), at(12), at(14))
    delivered(db, same_day, at(13))
    dated_order(db, TODAY, created_at=at(12, day=DAY - timedelta(days=1)))

    gate = report(client, manager_claim_headers, start_date=DAY.isoformat(), end_date=TODAY.isoformat())["gate"]

    assert (gate["excluded"]["dated"], gate["excluded"]["no_window"]) == (2, 0)
    assert (gate["sample"], gate["n"], gate["kept"]) == (3, 1, 1)


def test_a_dated_order_placed_before_the_range_is_dated_when_its_first_promise_is_in_it(
    rebase_clock, client, db, manager_claim_headers
):
    """Whether an order was held for a later day does not depend on the range. Placed the day before DAY for DAY, held,
    then released by the real sweep at 06:00 on DAY: its first engine row is in the range, promising 10:00-12:00, and
    it is still not delivered at 15:00 the next day. It is `dated`, as the same order placed inside the range is, and
    never a judged miss."""
    held = dated_order(db, DAY, created_at=at(12, day=DAY - timedelta(days=1)))
    assert release_due_scheduled_orders()["released"] == 1  # its day is behind the real clock, so it is due
    rebase_clock(at(6))  # the sweep ran at 06:00 on DAY
    db.session.expire_all()
    promised(db, db.session.get(Order, held.id), at(6, 5), at(10), at(12))

    gate = report(client, manager_claim_headers, **ONE_DAY)["gate"]

    assert gate["excluded"]["dated"] == 1
    assert (gate["sample"], gate["n"], gate["kept"]) == (1, 0, 0)


def test_a_failed_delivery_moved_to_a_later_day_stays_a_judged_miss(
    client, db, manager_claim_headers, admin_claim_headers, driver
):
    """A confirmed order for today, promised 10:00-12:00. Its driver picks it up and fails it at 11:30, and an admin
    moves it to tomorrow from the Reschedule modal, with the body the modal sends: the order's delivery date is now
    after the day it was placed, but it was never held for a later day. Its first promise is judged, and missed."""
    order = placed(db, driver)
    promised(db, order, at(9), at(10), at(12))
    tap(db, order.delivery, DeliveryStatus.PICKED_UP, at(10))
    tap(db, order.delivery, DeliveryStatus.FAILED, at(11, 30))

    moved = client.patch(
        RESCHEDULE.format(order.id),
        json={
            "delivery_date": (local_date() + timedelta(days=1)).isoformat(),  # the route judges it on the real clock
            "delivery_window_start": None,
            "delivery_window_end": None,
            "reason": "The customer was away",
        },
        headers=admin_claim_headers,
    )
    assert moved.status_code == 200, moved.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Order, order.id).delivery_date > DAY

    gate = report(client, manager_claim_headers, **ONE_DAY)["gate"]

    assert gate["excluded"]["dated"] == 0
    assert (gate["sample"], gate["n"], gate["kept"]) == (1, 1, 0)


# --------------------------------------------------------------------------------------------------------------------
# The figures below the gate
# --------------------------------------------------------------------------------------------------------------------


def test_a_tomorrow_promise_delivered_the_same_day_is_reported(client, db, manager_claim_headers, driver):
    """Two "tomorrow" promises made at 09:00 on DAY: one delivered that same afternoon (a falsely long promise), one
    the next morning. Two "today" promises: one delivered that day, one the next morning."""
    for window_day, delivered_at in (
        (TODAY, at(16)),
        (TODAY, at(11, day=TODAY)),
        (DAY, at(13)),
        (DAY, at(9, day=TODAY)),
    ):
        order = placed(db, driver)
        promised(db, order, at(9), at(12, day=window_day), at(14, day=window_day))
        delivered(db, order, delivered_at)

    figures = report(client, manager_claim_headers, **ONE_DAY)

    assert figures["tomorrow_delivered_same_day_share"] == pytest.approx(0.5)
    assert figures["today_delivered_today_share"] == pytest.approx(0.5)


def test_early_arrivals_are_reported_with_their_share(client, db, manager_claim_headers, driver):
    """Promised 12:00-14:00 and delivered at 11:00 and 11:30 (60 and 30 minutes early) and at 13:00. A window wider than
    four hours is shown as "by 14:00", with no start, so arriving before it is not early."""
    for delivered_at in (at(11), at(11, 30), at(13)):
        order = placed(db, driver)
        promised(db, order, at(9), at(12), at(14))
        delivered(db, order, delivered_at)
    by_only = placed(db, driver)
    promised(db, by_only, at(8, 30), at(9), at(14))
    delivered(db, by_only, at(8, 45))

    figures = report(client, manager_claim_headers, **ONE_DAY)

    assert figures["early_share"] == pytest.approx(0.5)
    assert figures["median_minutes_early"] == pytest.approx(45.0)


def test_the_window_and_capacity_figures_read_the_judged_first_promises(client, db, manager_claim_headers, driver):
    """Bucketed by stops ahead and whether the car had started, as the window was priced; by the cars working when it
    was made; and the median width overall and for orders placed 07:00-15:00 (spec §9)."""
    early_morning = placed(db, driver, placed_at=at(6, 30))
    promised(db, early_morning, at(6, 35), at(10), at(14), stops_ahead=4, started=False, drivers_working=1)
    delivered(db, early_morning, at(15))
    for stops_ahead in (0, 1):
        order = placed(db, driver, placed_at=at(9))
        promised(db, order, at(9, 5), at(11), at(12), stops_ahead=stops_ahead, drivers_working=2)
        delivered(db, order, at(11, 30))

    figures = report(client, manager_claim_headers, **ONE_DAY)

    assert figures["by_bucket"] == [
        pytest.approx(
            {"stops_ahead_band": "0", "started": True, "n": 1, "kept_share": 1.0, "median_window_minutes": 60.0}
        ),
        pytest.approx(
            {"stops_ahead_band": "1-2", "started": True, "n": 1, "kept_share": 1.0, "median_window_minutes": 60.0}
        ),
        pytest.approx(
            {"stops_ahead_band": "3-4", "started": False, "n": 1, "kept_share": 0.0, "median_window_minutes": 240.0}
        ),
    ]
    assert figures["median_window_minutes"] == pytest.approx({"overall": 60.0, "placed_7_to_15": 60.0})
    assert figures["by_drivers_working"] == {
        "one": pytest.approx({"n": 1, "kept_share": 0.0, "median_window_minutes": 240.0}),
        "two_or_more": pytest.approx({"n": 2, "kept_share": 1.0, "median_window_minutes": 60.0}),
    }


def test_the_buckets_come_in_band_order_with_a_car_not_started_first(client, db, manager_claim_headers, driver):
    """The page draws `by_bucket` in the order the report sends it: by stops-ahead band, and within a band the car not
    yet started before the one that had."""
    for stops_ahead, started in ((5, True), (0, True), (5, False), (0, False)):
        order = placed(db, driver)
        promised(db, order, at(9), at(10), at(12), stops_ahead=stops_ahead, started=started)
        delivered(db, order, at(11))

    buckets = report(client, manager_claim_headers, **ONE_DAY)["by_bucket"]

    assert [(row["stops_ahead_band"], row["started"]) for row in buckets] == [
        ("0", False),
        ("0", True),
        ("5-7", False),
        ("5-7", True),
    ]


def test_the_report_publishes_the_thresholds_its_copy_names(client, db, manager_claim_headers):
    """The page's copy names the morning gate (spec §5), the hours of the daytime median and the "on the way" tolerance
    (spec §9). The report sends them, so the page never states them on its own."""
    payload = report(client, manager_claim_headers, **ONE_DAY)

    assert payload["definitions"] == {
        "morning_ends": "11:00",
        "daytime_from": "07:00",
        "daytime_to": "15:00",
        "s2_tolerance_minutes": 15,
    }


def test_the_change_messages_the_rule_would_send_are_counted_per_order(client, db, manager_claim_headers, driver):
    """One judged order is never moved; one moves 45 minutes later at 10:00 (one message); one moves later twice, an
    hour apart (two). One more is promised for tomorrow and moved an hour later at 23:00: the quiet hours hold that
    change until 08:00, and the driver delivers it at 07:30, so it is never sent."""
    steady = placed(db, driver)
    promised(db, steady, at(9), at(12), at(14))
    once = placed(db, driver)
    promised(db, once, at(9), at(12), at(14))
    promised(db, once, at(10), at(12, 45), at(14, 45))
    twice = placed(db, driver)
    promised(db, twice, at(9), at(12), at(14))
    promised(db, twice, at(10), at(12, 45), at(14, 45))
    promised(db, twice, at(11), at(13, 30), at(15, 30))
    for order in (steady, once, twice):
        delivered(db, order, at(13))
    overnight = placed(db, driver)
    promised(db, overnight, at(9), at(10, day=TODAY), at(12, day=TODAY))
    promised(db, overnight, at(23), at(11, day=TODAY), at(13, day=TODAY))
    delivered(db, overnight, at(7, 30, day=TODAY))

    figures = report(client, manager_claim_headers, **ONE_DAY)

    assert figures["change_messages"] == pytest.approx({"n": 4, "none_share": 0.5, "at_most_one_share": 0.75})


def test_a_change_released_after_the_driver_set_off_is_not_counted(client, db, manager_claim_headers, driver):
    """Told at 10:00 that the end moved to 14:30; at 10:30 it moves to 15:00, within the hour, so the change waits for
    11:00. The driver sets off at 10:45, where the "on the way" promise takes over, and delivers at 11:15: the waiting
    stage's change was never sent."""
    order = placed(db, driver)
    promised(db, order, at(9), at(12), at(14))
    promised(db, order, at(10), at(12, 30), at(14, 30))
    promised(db, order, at(10, 30), at(13), at(15))
    tap(db, order.delivery, DeliveryStatus.PICKED_UP, at(10, 40))
    tap(db, order.delivery, DeliveryStatus.IN_TRANSIT, at(10, 45))
    delivered(db, order, at(11, 15))

    figures = report(client, manager_claim_headers, **ONE_DAY)

    assert figures["change_messages"] == pytest.approx({"n": 1, "none_share": 0.0, "at_most_one_share": 1.0})


def test_the_s2_figures_come_from_the_shared_leg_query(client, db, manager_claim_headers, driver, admin_user):
    """Phase 1's "on the way" legs of DAY, Google saying 18 minutes and our engine 11, judged by the arrival the driver
    tapped, as headroom reads them:
    - 10:00, by 10:29, arrived 10:23: kept, within 15 minutes of the by time; Google 5 minutes off, our engine 12;
    - 12:00, by 12:29, arrived 12:50, no shadow row: late, 21 minutes off the by time; Google 32 minutes off;
    - 18:00, by 18:29, arrived 18:05: kept, but 24 minutes before the by time; Google 13 minutes off, our engine 6;
    - 14:00, handed to another driver at 14:05: interrupted, so it is not judged;
    - 16:00, its arrival tapped by an admin: not the driver's arrival, so it is not judged;
    - and a leg of the day before the range."""
    leg(db, driver, at(10))
    leg(db, driver, at(12), drive=50, shadow=None)
    handed_over = leg(db, driver, at(14), drive=None)
    second = make_driver(db)
    reassign(db, handed_over.delivery, second, at(14, 5), by=admin_user)
    tap(db, handed_over.delivery, DeliveryStatus.ARRIVED, at(14, 40))
    leg(db, driver, at(16), arrived_by=admin_user)
    leg(db, driver, at(18), drive=5)
    leg(db, driver, at(10, day=DAY - timedelta(days=1)))

    s2 = report(client, manager_claim_headers, **ONE_DAY)["s2"]

    assert s2 == {
        "n": 3,
        "kept_share": pytest.approx(2 / 3),
        "within_15_share": pytest.approx(1 / 3),
        "google_median_error_minutes": pytest.approx(13.0),
        "engine_n": 2,
        "engine_median_error_minutes": pytest.approx(9.0),
    }


def test_promises_from_an_old_position_are_judged_on_their_own_line(client, db, manager_claim_headers, driver):
    """A tap from a position too old for Google is promised our engine's minutes plus an allowance (`stale_estimate`,
    owner commit 0650789). The customer is told it, so it is judged, on its own line beside Google's legs:
    - 11:00, our engine 30 minutes, by 12:00, arrived 11:40: kept, 20 minutes before the by time; engine 10 off;
    - 13:00, our engine 20 minutes, by 13:40, arrived 13:50: late, 10 minutes after it; engine 30 off;
    - and one Google leg at 10:00, which stays alone on Google's line."""
    leg(db, driver, at(10))
    stale_leg(db, driver, at(11), engine=30, by_minutes=60, drive=40)
    stale_leg(db, driver, at(13), engine=20, by_minutes=40, drive=50)

    payload = report(client, manager_claim_headers, **ONE_DAY)

    assert (payload["s2"]["n"], payload["s2"]["engine_n"]) == (1, 1)
    assert payload["s2_stale_estimate"] == {
        "n": 2,
        "kept_share": pytest.approx(1 / 2),
        "within_15_share": pytest.approx(1 / 2),
        "engine_median_error_minutes": pytest.approx(20.0),
    }


@pytest.mark.usefixtures("spec_tunables")
def test_headroom_is_unchanged_over_the_same_legs(client, db, manager_claim_headers, driver):
    """30 legs, each 23 minutes against Google's 18 (r = (23 - 5) / 18 = 1.0), an hour apart up to NOW; our engine's
    shadow on every other one. Headroom learns from all 30 as Phase 1 always has, (30 x 1.0 + 20 x 1.3) / 50 = 1.12,
    and the report judges the same 30: a leg without a shadow stays, and none is counted twice."""
    for n in range(30):
        leg(db, driver, NOW - timedelta(hours=n + 1), shadow=11 if n % 2 else None)

    s2 = report(client, manager_claim_headers, start_date=DAY.isoformat(), end_date=TODAY.isoformat())["s2"]

    assert (s2["n"], s2["engine_n"]) == (30, 15)
    assert DeliveryEtaService().headroom(now=NOW) == pytest.approx((30 * 1.0 + 20 * 1.3) / 50)


# --------------------------------------------------------------------------------------------------------------------
# The payload, its access, and the page's fixture
# --------------------------------------------------------------------------------------------------------------------


def _every_figure(db, driver):
    """A day with every figure the page shows: one order per gate category; judged orders in two buckets, with one and
    with two cars working, a "tomorrow" promise delivered the same day, an early arrival and a moved promise; two
    on-the-way legs, one with our engine's shadow; and the weekly fit with its monitoring."""
    add_fit(db, FIT_MONDAY, monitoring=MONITORING)
    kept, _ = _one_order_per_category(db, driver)
    promised(db, kept, at(10), at(10, 45), at(12, 45))  # a move of 45 minutes: one message
    falsely_long = placed(db, driver, placed_at=at(7, 30))
    promised(db, falsely_long, at(7, 35), at(10, day=TODAY), at(13, day=TODAY), stops_ahead=3, started=False)
    delivered(db, falsely_long, at(16))
    early = placed(db, driver)
    promised(db, early, at(9), at(12), at(14), drivers_working=2)
    delivered(db, early, at(11, 15))
    leg(db, driver, at(10))
    leg(db, driver, at(12), drive=50, shadow=None)
    stale_leg(db, driver, at(11), engine=30, by_minutes=60, drive=40)


def test_no_report_key_is_rewritten_by_the_timezone_middleware(client, db, manager_claim_headers, driver):
    """The middleware rewrites an ISO value under any key on its list into the reader's zone. The report crosses it
    unchanged, and none of its keys, at any depth, is on the list."""
    _every_figure(db, driver)

    payload = report(client, manager_claim_headers, **ONE_DAY)

    assert payload == json.loads(json.dumps(EtaAccuracyReport.build(start_date=DAY, end_date=DAY, now=NOW)))
    instant = "2026-11-03T09:00:00+00:00"
    probe = {key: instant for key in _keys(payload)}
    assert TimezoneMiddleware()._convert_response_datetimes(probe, DISPLAY_TIMEZONE) == probe


def _keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys(inner)


@pytest.mark.parametrize(
    "role, expected",
    [
        pytest.param("delivery_driver", 403, id="driver"),
        pytest.param("operator", 403, id="operator"),
        pytest.param("manager", 200, id="manager"),
        pytest.param("admin", 200, id="admin"),
    ],
)
def test_a_driver_gets_403_and_a_manager_gets_the_report(
    app, client, db, driver, operator_user, manager_user, admin_user, admin_claim_headers, role, expected
):
    """The route answers admins and managers only, and the permission the admin UI shows the page by says the same."""
    people = {"delivery_driver": driver, "operator": operator_user, "manager": manager_user, "admin": admin_user}
    user = people[role]
    headers = admin_claim_headers if role == "admin" else claim_headers(app, user, role)

    response = client.get(REPORT, query_string=ONE_DAY, headers=headers)

    assert response.status_code == expected, response.get_data(as_text=True)
    assert AuthService().get_user_permissions(user.id)["can_view_eta_accuracy"] is (expected == 200)
    if expected == 200:
        assert (response.get_json()["data"]["start_date"], response.get_json()["data"]["end_date"]) == (
            "2026-11-03",
            "2026-11-03",
        )


def test_without_a_range_the_report_covers_the_last_fourteen_days(client, db, manager_claim_headers):
    payload = report(client, manager_claim_headers)

    assert (payload["start_date"], payload["end_date"]) == ("2026-10-22", "2026-11-04")


def test_the_admin_page_fixture_is_this_scenarios_report(client, db, manager_claim_headers, driver):
    """The page's fixture holds every figure the page shows, none empty, as the route answers it today."""
    _every_figure(db, driver)

    payload = report(client, manager_claim_headers, **ONE_DAY)

    gate = payload["gate"]
    assert gate["n"] and gate["kept_share"] is not None and gate["before_eleven"]["kept_share"] is not None
    assert all(count == 1 for count in gate["excluded"].values()) and isinstance(gate["passes"], bool)
    assert {(row["stops_ahead_band"], row["started"]) for row in payload["by_bucket"]} >= {("0", True), ("3-4", False)}
    for name in (
        "today_delivered_today_share",
        "tomorrow_delivered_same_day_share",
        "early_share",
        "median_minutes_early",
    ):
        assert payload[name] is not None, name
    assert None not in payload["median_window_minutes"].values()
    for group in payload["by_drivers_working"].values():
        assert group["n"] and group["kept_share"] is not None and group["median_window_minutes"] is not None
    assert payload["change_messages"]["at_most_one_share"] is not None and payload["change_messages"]["none_share"] < 1
    assert payload["s2"]["n"] and payload["s2"]["engine_n"]
    assert None not in payload["s2_stale_estimate"].values()
    assert payload["latest_fit"]["monitoring"] == MONITORING
    if not FIXTURE.exists():
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    assert json.loads(FIXTURE.read_text("utf-8")) == payload, (
        f"{FIXTURE.name} is stale: delete it, re-run this test to write it again, then re-run the admin UI's "
        "EtaAccuracy test"
    )


def test_the_report_runs_on_postgres(pg_app, pg_db):
    """The same day on a fully migrated Postgres, where the first rows are ranked by a window function over
    timestamptz, the statuses are enums and the fit's JSON reads back: the route answers what the page's fixture
    holds."""
    _every_figure(pg_db, make_driver(pg_db))
    manager = make_customer(pg_db, first_name="Malika", role=UserRole.MANAGER, user_type=UserType.STAFF)

    response = pg_app.test_client().get(REPORT, query_string=ONE_DAY, headers=claim_headers(pg_app, manager, "manager"))

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"] == json.loads(FIXTURE.read_text("utf-8"))
