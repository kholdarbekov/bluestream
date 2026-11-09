"""The recorder of the capacity-aware ETA plan (honest-ETA Phase 2, Task 6): the one writer of the hidden S1 promises.

Each test hands `record` the plan the engine would hand it, `PlannedStop`s as the planner (Task 5) gives them, and the
`now` of the run, then commits as the engine does. The stops point at real delivery rows (a promise needs one), built
with tests/integration/eta_plan_builders.py, and every window is measured with Task 4's own rules (`lo_hi`,
`customer_bounded_window`) or pinned on the Tashkent wall clock. `TODAY` is Wednesday 2026-11-04, so nothing depends
on when the suite runs. A stop with no car comes from `build_plan` itself, with no cars, so the no-car rules are the
planner's own.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Iterable, List, Optional, Tuple

import pytest

from business_app import db as _db
from business_app.models.delivery_promise import DeliveryPromise
from business_app.models.order import Order
from business_app.services.eta_plan import recorder
from business_app.services.eta_plan.demand import Stop
from business_app.services.eta_plan.params import EtaParams, current_params
from business_app.services.eta_plan.planner import PlannedStop, build_plan
from business_app.services.eta_plan.recorder import record
from business_app.services.eta_plan.windows import customer_bounded_window, day_kind, lo_hi
from business_app.utils.local_windows import work_day_of
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import DeliveryStatus
from tests.integration.eta_plan_builders import dated_order, make_driver, order_at_km
from tests.integration.reorder_reminder_builders import captured_logs
from tests.unit.test_eta_plan_planner import DEPOT, SEED, TODAY, at, km
from tests.unit.test_eta_plan_stop_time_windows import MONDAYS, add_fit

pytestmark = pytest.mark.unit

TOMORROW = TODAY + timedelta(days=1)
NOW = at("10:00")
# The engine's rows, as the accuracy report and the gate select them.
ENGINE_MODEL = "engine_s1_v1"
WAITING = "waiting"


def _order(db, km_out: float = 2.0, *, driver=None) -> Order:
    """A real order and delivery for a promise to point at: a pool order `km_out` km from the depot, or one `driver`
    holds."""
    return order_at_km(db, km_out, created_at=at("09:00"), driver=driver)


def _stop(
    order: Order,
    *,
    status: DeliveryStatus = DeliveryStatus.SCHEDULED,
    day: Optional[date] = None,
    window: Tuple[Optional[datetime], Optional[datetime]] = (None, None),
) -> Stop:
    """`order` as demand hands it to the planner (R7): `day` is the customer's date, `window` their own window as
    instants. A dated order held for its day has no delivery row, so no delivery id and no status. The recorder reads
    the delivery, its status, the date and the window; the other fields are the planner's."""
    delivery = order.delivery
    return Stop(
        delivery_id=delivery.id if delivery is not None else None,
        order_id=order.id,
        status=status if delivery is not None else None,
        driver_id=delivery.delivery_person_id if delivery is not None else None,
        point=None,
        plan_day=day if day is not None and day > TODAY else TODAY,
        customer_date=day,
        created_at=at("09:00"),
        window_start=window[0],
        window_end=window[1],
        not_before=window[0],
        bottles=1,
        pool_rank=1,
        committed=False,
        route_index=None,
    )


def _timed(stop: Stop, expected_at: datetime, *, stops_ahead: int = 0, started: bool = False) -> PlannedStop:
    """`stop` on a car's plan: reached at `expected_at`, `stops_ahead` stops ahead of it, the car started or not."""
    return PlannedStop(
        stop=stop,
        driver_id=1,
        plan_day=work_day_of(expected_at),
        expected_at=expected_at,
        stops_ahead=stops_ahead,
        day_started=started,
        reload_before=False,
        orphan=False,
    )


def _without_a_car(app, stop: Stop, now: datetime) -> PlannedStop:
    """`stop` as the planner leaves it when no car can take it: unconfirmed, or keeping its customer's later date."""
    (planned,) = build_plan({}, [stop], SEED, now, DEPOT, km=km, config=app.config)
    assert planned.expected_at is None
    return planned


def _record(planned: Iterable[PlannedStop], now: datetime, *, params: EtaParams = SEED, drivers_working: int = 2):
    """One run's recording: `record`, then the engine's commit."""
    recorded = record(list(planned), params, now, drivers_working)
    _db.session.commit()
    return recorded


def _rows(order: Order) -> List[DeliveryPromise]:
    """The engine rows of `order`'s delivery, oldest first."""
    return (
        DeliveryPromise.query.filter_by(delivery_id=order.delivery.id, model_version=ENGINE_MODEL)
        .order_by(DeliveryPromise.computed_at, DeliveryPromise.id)
        .all()
    )


def _window(row: DeliveryPromise) -> Optional[Tuple[datetime, datetime]]:
    if row.window_start is None and row.window_end is None:
        return None
    return ensure_utc(row.window_start), ensure_utc(row.window_end)


def _r11_window(stop: Stop, expected_at: datetime, now: datetime, *, stops_ahead=0, started=False, params=SEED):
    """The window R11 records for `stop` reached at `expected_at`, by Task 4's rules."""
    lo, hi, _ = lo_hi(params, stops_ahead, started)
    return customer_bounded_window(expected_at, lo, hi, now, stop.window_start, stop.window_end)


@pytest.mark.parametrize("kind", ["timed", "dated", "unconfirmed"])
def test_a_first_promise_is_always_recorded(app, db, kind):
    """Whatever the plan says of a delivery with no engine row yet, one row is written: hidden, `waiting`, tied to no
    tap, stamped with the run's `now`, the cars working and the parameters used."""
    if kind == "dated":
        day = TODAY + timedelta(days=5)
        order = dated_order(db, day, created_at=at("09:00"), parked=True)
        planned = _without_a_car(app, _stop(order, status=DeliveryStatus.RESCHEDULED, day=day), NOW)
    else:
        order = _order(db)
        stop = _stop(order)
        planned = _timed(stop, at("12:00")) if kind == "timed" else _without_a_car(app, stop, NOW)

    assert _record([planned], NOW) == 1

    (row,) = _rows(order)
    assert (row.model_version, row.stage, row.shown, row.delivery_status_history_id) == (
        ENGINE_MODEL,
        WAITING,
        False,
        None,
    )
    assert ensure_utc(row.computed_at) == NOW
    assert (row.drivers_working, row.source) == (2, "seed")
    if kind == "timed":
        assert ensure_utc(row.expected_at) == at("12:00")
        assert _window(row) == _r11_window(planned.stop, at("12:00"), NOW)
        assert (row.day_kind, row.promised_date, row.stops_ahead, row.day_started) == ("today", TODAY, 0, False)
    elif kind == "dated":
        assert (row.expected_at, _window(row), row.stops_ahead) == (None, None, None)
        assert (row.day_kind, row.promised_date) == ("date", TODAY + timedelta(days=5))
    else:
        assert (row.expected_at, _window(row), row.stops_ahead) == (None, None, None)
        assert (row.day_kind, row.promised_date) == ("unconfirmed", None)


def test_a_window_end_moving_less_than_fifteen_minutes_records_nothing(app, db, count_queries):
    """Five minutes on, one stop's E has slid ten minutes inside the same half hour, so its rounded window ends where it
    did; another's customer moved the end of their own window ten minutes later; a third has not moved. Nothing is
    written, and the latest rows of all three are read in one query."""
    slid, widened, still = (_order(db, km_out) for km_out in (2.0, 3.0, 4.0))
    first = [
        _timed(_stop(slid), at("12:00")),
        _timed(_stop(widened, window=(at("11:00"), at("14:00"))), at("12:00")),
        _timed(_stop(still), at("13:00")),
    ]
    assert _record(first, NOW) == 3
    later = NOW + timedelta(minutes=5)
    second = [
        _timed(_stop(slid), at("12:10")),
        _timed(_stop(widened, window=(at("11:00"), at("14:10"))), at("12:00")),
        _timed(_stop(still), at("13:00")),
    ]
    # The slide stays inside the half hour the window end is rounded to.
    assert _r11_window(second[0].stop, at("12:10"), later)[1] == _window(_rows(slid)[0])[1]

    with count_queries() as counter:
        assert record(second, SEED, later, 2) == 0
    _db.session.commit()

    assert [len(_rows(order)) for order in (slid, widened, still)] == [1, 1, 1]
    assert len([sql for sql in counter.statements if "delivery_promises" in sql]) == 1


def test_fifteen_minutes_or_more_records_a_new_row(app, db):
    """One customer moves the end of their window by exactly fifteen minutes; another stop's E slides into the next
    half hour, so its window end moves by thirty. Each gets a new row, and the next move is measured from that row,
    the latest by (computed_at, id), not from the first."""
    widened, slid = _order(db, 2.0), _order(db, 3.0)
    _record(
        [_timed(_stop(widened, window=(at("11:00"), at("14:00"))), at("12:00")), _timed(_stop(slid), at("12:00"))],
        NOW,
    )
    later = NOW + timedelta(minutes=5)

    assert (
        _record(
            [
                _timed(_stop(widened, window=(at("11:00"), at("14:15"))), at("12:00")),
                _timed(_stop(slid), at("12:20")),
            ],
            later,
        )
        == 2
    )

    assert [_window(row)[1] for row in _rows(widened)] == [at("14:00"), at("14:15")]
    first_end, second_end = (_window(row)[1] for row in _rows(slid))
    assert second_end - first_end == timedelta(minutes=30)
    # Ten minutes from the latest row's 14:15 is no move worth a row, though it is twenty-five from the first's 14:00.
    stop = _stop(widened, window=(at("11:00"), at("14:25")))
    assert _record([_timed(stop, at("12:00"))], later + timedelta(minutes=5)) == 0


def test_a_promised_date_change_records_a_new_row(app, db):
    """A late stop's E slips past midnight while its window still ends in the same half hour: the promised day moves,
    and a row is written. So is a dated row without a time whose customer moves it a day."""
    evening = at("20:00")
    late = _order(db)
    day = TODAY + timedelta(days=5)
    dated = dated_order(db, day, created_at=at("09:00"), parked=True)
    first = [
        _timed(_stop(late), at("23:50"), started=True),
        _without_a_car(app, _stop(dated, status=DeliveryStatus.RESCHEDULED, day=day), evening),
    ]
    assert _record(first, evening) == 2
    later = evening + timedelta(minutes=5)
    moved = day + timedelta(days=1)
    second = [
        _timed(_stop(late), at("00:05", TOMORROW), started=True),
        _without_a_car(app, _stop(dated, status=DeliveryStatus.RESCHEDULED, day=moved), later),
    ]

    assert _record(second, later) == 2

    late_rows = _rows(late)
    assert [row.promised_date for row in late_rows] == [TODAY, TOMORROW]
    assert _window(late_rows[0])[1] == _window(late_rows[1])[1]  # the window end alone would record nothing
    dated_rows = _rows(dated)
    assert [(row.promised_date, _window(row)) for row in dated_rows] == [(day, None), (moved, None)]


def test_midnight_alone_records_nothing(app, db):
    """ "Tomorrow 12:00-15:00", promised at 20:00, is "today 12:00-15:00" at 00:05: the same window on the same day.
    Only the label changed, so nothing is written, and the stored label stays the one of its `computed_at`."""
    order = _order(db)
    stop = _stop(order, day=TOMORROW, window=(at("12:00", TOMORROW), at("15:00", TOMORROW)))
    expected_at = at("13:00", TOMORROW)
    assert _record([_timed(stop, expected_at)], at("20:00")) == 1
    after_midnight = at("00:05", TOMORROW)
    assert day_kind(expected_at, after_midnight) == "today"  # what a row written now would say

    assert _record([_timed(stop, expected_at)], after_midnight) == 0

    (row,) = _rows(order)
    assert _window(row) == (at("12:00", TOMORROW), at("15:00", TOMORROW))
    assert (row.day_kind, row.promised_date) == ("tomorrow", TOMORROW)


def test_a_window_crossing_midnight_keeps_its_promised_date(app, db):
    """E at 23:40 under a fit whose "0|started" bucket spans -20 to +80 minutes: the window runs from 23:00 to 01:00.
    The promised day is E's, today, not the day the window ends on."""
    add_fit(db, MONDAYS[3], buckets={"0|started": {"lo": -20, "hi": 80, "n": 40}})
    evening = at("20:00")
    order = _order(db)

    _record([_timed(_stop(order), at("23:40"), started=True)], evening, params=current_params(evening))

    (row,) = _rows(order)
    assert _window(row) == (at("23:00"), at("01:00", TOMORROW))
    assert (row.day_kind, row.promised_date) == ("today", TODAY)


def test_in_transit_and_arrived_stops_get_no_s1_row(app, db):
    """A stop the driver has set off to, or reached, is S2's: its promise is the Google tap's (spec §4.8)."""
    driver = make_driver(db)
    on_the_way, at_the_door, assigned = (_order(db, km_out, driver=driver) for km_out in (1.0, 2.0, 3.0))
    planned = [
        _timed(_stop(on_the_way, status=DeliveryStatus.IN_TRANSIT), at("10:20"), started=True),
        _timed(_stop(at_the_door, status=DeliveryStatus.ARRIVED), at("10:00"), started=True),
        _timed(_stop(assigned, status=DeliveryStatus.ASSIGNED), at("10:45"), stops_ahead=1, started=True),
    ]

    assert _record(planned, NOW) == 1

    assert (_rows(on_the_way), _rows(at_the_door)) == ([], [])
    assert [row.stage for row in _rows(assigned)] == [WAITING]


def test_a_picked_up_stop_still_gets_s1_rows(app, db):
    """S2 starts at the in_transit tap (spec §4.8): a picked-up stop still waits for its leg, so its S1 promise is
    recorded, and recorded again when it moves."""
    order = _order(db, driver=make_driver(db))
    stop = _stop(order, status=DeliveryStatus.PICKED_UP)

    assert _record([_timed(stop, at("11:00"), stops_ahead=1, started=True)], NOW) == 1
    assert _record([_timed(stop, at("12:00"), stops_ahead=1, started=True)], NOW + timedelta(minutes=10)) == 1

    assert [(row.stage, ensure_utc(row.expected_at)) for row in _rows(order)] == [
        (WAITING, at("11:00")),
        (WAITING, at("12:00")),
    ]


def test_engine_rows_name_their_parameters(app, db):
    """A row's `source` names the parameters it was planned with: the seeds before any fit, then the fit's id, so the
    accuracy gate judges a fit only on promises made with it (R12)."""
    before_the_fit, after_the_fit = _order(db, 2.0), _order(db, 3.0)
    _record([_timed(_stop(before_the_fit), at("12:00"))], NOW, params=current_params(NOW))
    fit = add_fit(db, MONDAYS[3])

    _record([_timed(_stop(after_the_fit), at("12:00"))], NOW, params=current_params(NOW))

    assert [row.source for row in _rows(before_the_fit)] == ["seed"]
    assert [row.source for row in _rows(after_the_fit)] == [f"fit:{fit.id}"]


@pytest.mark.parametrize("on_its_day", [True, False], ids=["a-car-takes-it-on-its-day", "no-car-on-its-day"])
def test_a_rescheduled_row_records_its_date(app, db, on_its_day):
    """Spec §7, "Rescheduled shows the date": a delivery rescheduled two days out is labelled with its date, with the
    plan's window when a car takes it on that day and without one when none does."""
    day = TODAY + timedelta(days=2)
    order = dated_order(db, day, created_at=at("09:00"), parked=True)
    stop = _stop(order, status=DeliveryStatus.RESCHEDULED, day=day)
    planned = _timed(stop, at("14:00", day)) if on_its_day else _without_a_car(app, stop, NOW)

    _record([planned], NOW)

    (row,) = _rows(order)
    assert (row.day_kind, row.promised_date) == ("date", day)
    assert (_window(row) is not None) is on_its_day


def test_a_dated_row_beyond_the_horizon_records_its_date_without_a_window(app, db):
    """A delivery the customer dated a week out lies beyond every plan day (ETA_UNCONFIRMED_HORIZON_DAYS): it keeps its
    date, with no time and no window (R10)."""
    day = TODAY + timedelta(days=7)
    order = dated_order(db, day, created_at=at("09:00"), parked=True)
    planned = _without_a_car(app, _stop(order, status=DeliveryStatus.RESCHEDULED, day=day), NOW)
    assert planned.plan_day == day

    _record([planned], NOW)

    (row,) = _rows(order)
    assert (row.expected_at, _window(row), row.stops_ahead) == (None, None, None)
    assert (row.day_kind, row.promised_date) == ("date", day)


@pytest.mark.parametrize("bound", ["ends-at-its-start", "ends-before-its-start"])
def test_a_window_that_would_not_end_after_it_starts_is_skipped_and_logged(app, db, caplog, bound):
    """The planner never places E before now, so a recorded window always ends after it starts. One that would not
    would trip the table's window CHECK and abort the whole run's insert: it is skipped and logged instead, and every
    other row of the run is written."""
    good, bad = _order(db, 2.0), _order(db, 3.0)
    if bound == "ends-at-its-start":
        # "Between", reached exactly at its end, which is now.
        bad_planned = _timed(_stop(bad, window=(at("09:00"), NOW)), NOW)
    else:
        # "Until" an end already past, reached before now.
        bad_planned = _timed(_stop(bad, window=(None, NOW - timedelta(minutes=10))), NOW - timedelta(minutes=15))

    with captured_logs(caplog, recorder.logger, logging.WARNING):
        assert _record([_timed(_stop(good), at("12:00")), bad_planned], NOW) == 1

    assert _rows(bad) == []
    assert all(ensure_utc(row.window_end) > ensure_utc(row.window_start) for row in DeliveryPromise.query.all())
    skipped = [line.getMessage() for line in caplog.records if line.getMessage().startswith("eta_plan_window_skipped")]
    assert len(skipped) == 1 and f"delivery={bad.delivery.id} " in skipped[0]


def test_a_delivery_the_run_did_not_plan_records_nothing(app, db):
    """A stop can drop out of one run: a dated order leaves demand at its release and returns with the sweep's pool
    row (R7). It gets no removal and no "unconfirmed" row; its latest promise stands until a run plans it again."""
    kept, absent = _order(db, 2.0), _order(db, 3.0)
    _record([_timed(_stop(kept), at("12:00")), _timed(_stop(absent), at("12:30"))], NOW)

    assert _record([_timed(_stop(kept), at("13:00"))], NOW + timedelta(minutes=10)) == 1

    assert [(row.day_kind, ensure_utc(row.expected_at)) for row in _rows(absent)] == [("today", at("12:30"))]


def test_a_dated_order_without_a_delivery_row_records_nothing(app, db):
    """A dated order held for its day with no delivery row yet takes a car's time on that day (R7), but a promise
    belongs to a delivery: nothing is written for it."""
    day = TOMORROW
    order = dated_order(db, day, created_at=at("09:00"))
    assert order.delivery is None

    assert _record([_timed(_stop(order, day=day), at("11:00", day))], NOW) == 0

    assert DeliveryPromise.query.count() == 0
