"""The weekly fit of the capacity-aware ETA plan (honest-ETA Phase 2, Task 8): what 28 plan days of real deliveries say
about stop times and promise windows (R5, R13, R14).

Each test builds the history the fit reads with tests/integration/eta_plan_builders.py (drivers, orders at a known
distance from the depot, the taps of a worked day) and the engine's own rows, hands `fit` its `now`, and reads the
result from the `eta_model_fits` row, or through `current_params` and `lo_hi` as the engine reads it. `MONDAY` is
2026-11-02: the fit runs at 05:30 on a Monday in Tashkent and reads the 28 plan days from 2026-10-05 to 2026-11-01.
Every instant is built, never read from the clock, so nothing depends on the time of day.

A leg's distance is the gap between two doors due north of the depot (`point_at_km`), so a stop at 1.0 km and the next
at 5.0 km are a 4.0 km leg. The tests of the 30-leg, 15-pair and 30-promise minimums build exactly that many; the tests
of the curve's shape lower the bin minimum instead, since they are about the fitted shape and not about the minimum.
"""

from datetime import date, time, timedelta
from typing import List, NamedTuple, Optional

import pytest
from celery.schedules import crontab

from business_app.models.delivery_promise import MODEL_ENGINE_S1, MODEL_GOOGLE_TAP, DeliveryPromise
from business_app.models.eta_model_fit import EtaModelFit
from business_app.services.eta_plan import calibration
from business_app.services.eta_plan.calibration import EXCLUDED_REASONS, EtaCalibration, actual_arrivals, bin_index
from business_app.services.eta_plan.params import current_params
from business_app.services.eta_plan.stop_time import cycle_minutes
from business_app.services.eta_plan.windows import bucket_key, lo_hi
from business_app.tasks import delivery_eta_tasks
from business_app.utils.delivery_window import window_edge_utc
from business_app.utils.local_windows import (
    WORK_DAY_START_HOUR,
    local_instant_at_minutes,
    work_day_bounds,
    work_day_of,
)
from business_app.utils.timezone_utils import ensure_utc
from shared import business_config
from shared.constants import ETA_STAGE_ON_THE_WAY, ETA_STAGE_WAITING
from shared.enums import DeliveryStatus
from tests.integration.eta_plan_builders import (  # noqa: F401 -- celery_app_module_fixture is the fixture
    add_fit,
    celery_app_module_fixture,
    close_session,
    make_driver,
    open_session,
    order_at_km,
    reassign,
    tap,
)

pytestmark = pytest.mark.unit

MONDAY = date(2026, 11, 2)
NOW = local_instant_at_minutes(MONDAY, 5 * 60 + 30)  # 05:30 in Tashkent: when the beat runs
FIRST_DAY = MONDAY - timedelta(days=28)  # the oldest plan day the fit reads
SEEDS = business_config.ETA_CYCLE_SEED_POINTS
SEED_LINE = business_config.ETA_DEPOT_LEG_SEED
# A previous fit with points of its own, to tell the previous fit's values from the seeds'.
PREVIOUS_POINTS = ((0.1, 14.0), (0.8, 17.0), (2.3, 25.0), (4.6, 33.0), (7.7, 45.0), (13.2, 68.0))

OWN = "the delivery's own driver"  # `Drop.by`
SOMEONE_ELSE = "a colleague, not the delivery's driver"  # `Drop.by`
LOADED = "when the day's load was"  # `Drop.picked_up`


def plan_day(n: int) -> date:
    """The n-th plan day the fit reads: 0 is the oldest, 27 the last."""
    return FIRST_DAY + timedelta(days=n)


class Drop(NamedTuple):
    """One stop of a worked day: `km` from the depot, delivered `at` minutes past the plan day's local midnight.

    - `bottles`: the order's returnable bottles.
    - `picked_up`: minutes past midnight, `LOADED` (when the day's load was), or None for a stop with no pick-up tap.
    - `by`: who taps it delivered: `OWN` (the delivery's driver), `SOMEONE_ELSE`, or None (nobody: the Orders screen).
    - `door`: False for an address without coordinates.
    - `reassigned`: minutes past midnight of a later re-save of the assignment, which writes a delivered -> delivered
      row by the driver.
    """

    km: float
    at: float
    bottles: int = 1
    picked_up: object = LOADED
    by: object = OWN
    door: bool = True
    reassigned: Optional[float] = None


def worked(db, driver, day, drops, *, loaded=540):
    """`driver`'s plan day `day`: an order per drop, picked up when the day's load was (`loaded`, minutes past the
    day's local midnight) unless the drop says otherwise, and delivered at the drop's time. Returns the orders."""
    orders = []
    for drop in drops:
        order = order_at_km(
            db,
            drop.km,
            created_at=local_instant_at_minutes(day, loaded) - timedelta(hours=1),
            driver=driver,
            bottles=drop.bottles,
        )
        if not drop.door:
            order.delivery_address.latitude = order.delivery_address.longitude = None
            db.session.commit()
        if drop.picked_up is not None:
            picked_up = loaded if drop.picked_up is LOADED else drop.picked_up
            tap(db, order.delivery, DeliveryStatus.PICKED_UP, local_instant_at_minutes(day, picked_up))
        if drop.by == OWN:
            by = driver
        elif drop.by == SOMEONE_ELSE:
            by = make_driver(db)
        else:
            by = drop.by
        tap(db, order.delivery, DeliveryStatus.DELIVERED, local_instant_at_minutes(day, drop.at), by=by)
        if drop.reassigned is not None:
            reassign(db, order.delivery, driver, local_instant_at_minutes(day, drop.reassigned), by=driver)
        orders.append(order)
    return orders


def alternating(low: float, high: float, count: int, *, first: float, every: float) -> List[Drop]:
    """`count` stops alternating between `low` and `high` km from the depot, `every` minutes apart from `first`: each
    leg is `high - low` km."""
    return [Drop(high if n % 2 else low, first + n * every) for n in range(count)]


def promise(db, delivery, expected_at, *, stops_ahead=1, started=True, computed_at=None, **row) -> DeliveryPromise:
    """An engine row for `delivery` expecting it at `expected_at`, as the recorder writes it. `row` overrides any of its
    other columns."""
    values = {
        "stage": ETA_STAGE_WAITING,
        "model_version": MODEL_ENGINE_S1,
        "shown": False,
        "computed_at": computed_at or local_instant_at_minutes(plan_day(10), 7 * 60),
        "stops_ahead": stops_ahead,
        "day_started": started,
        **row,
    }
    created = DeliveryPromise(delivery_id=delivery.id, expected_at=expected_at, **values)
    db.session.add(created)
    db.session.commit()
    return created


def late_by(db, delivery, arrival, minutes_late, *, stops_ahead=1, started=True) -> None:
    """One engine row per entry of `minutes_late`, each expecting `delivery` that many minutes before `arrival`."""
    db.session.add_all(
        DeliveryPromise(
            delivery_id=delivery.id,
            expected_at=arrival - timedelta(minutes=late),
            stage=ETA_STAGE_WAITING,
            model_version=MODEL_ENGINE_S1,
            shown=False,
            computed_at=local_instant_at_minutes(plan_day(10), 7 * 60),
            stops_ahead=stops_ahead,
            day_started=started,
        )
        for late in minutes_late
    )
    db.session.commit()


def only_fit() -> EtaModelFit:
    return EtaModelFit.query.one()


def kept_bins(fit: EtaModelFit):
    return {key for key in fit.kept_previous if key.startswith("bin:")}


def nonzero(excluded):
    return {reason: count for reason, count in excluded.items() if count}


# ---------------------------------------------------------------------------------------------------------------------
# The window of plan days, a second run, the beat and the task.
# ---------------------------------------------------------------------------------------------------------------------


def test_the_fit_reads_the_28_plan_days_before_the_plan_day_it_runs_on(db):
    fit = EtaCalibration().fit(NOW)

    assert ensure_utc(fit.data_to) == work_day_bounds(work_day_of(NOW))[0] == window_edge_utc(MONDAY, time(5, 0))
    assert ensure_utc(fit.data_from) == work_day_bounds(plan_day(0))[0] == window_edge_utc(FIRST_DAY, time(5, 0))
    assert ensure_utc(fit.fitted_at) == NOW


@pytest.mark.parametrize(
    "day, drops, loaded, legs",
    [
        pytest.param(
            plan_day(0),
            [Drop(2.0, 299), Drop(2.5, 300), Drop(3.0, 320)],
            310,
            1,
            id="04:59 on the first day belongs to the day before the window: only 05:00 to 05:20 is a leg",
        ),
        pytest.param(
            plan_day(27),
            [Drop(2.0, 1700), Drop(2.5, 1739), Drop(3.0, 1740)],
            600,
            1,
            id="05:00 on the Monday it runs is the next plan day: 04:59 is the last minute read",
        ),
        pytest.param(
            plan_day(5),
            [Drop(2.0, 1420), Drop(2.5, 1460)],
            600,
            1,
            id="a plan day runs past midnight: 23:40 and 00:20 are one leg",
        ),
        pytest.param(
            plan_day(5),
            [Drop(2.0, 1730), Drop(2.5, 1750)],
            600,
            0,
            id="a plan day ends at 05:00: 04:50 and 05:10 are two days, so no leg",
        ),
    ],
)
def test_the_window_and_each_plan_day_end_at_5_am(db, day, drops, loaded, legs):
    worked(db, make_driver(db), day, drops, loaded=loaded)

    assert EtaCalibration().fit(NOW).rows_used["cycle"] == legs


def test_a_second_run_for_the_same_window_inserts_nothing(db, monkeypatch):
    first = EtaCalibration().fit(NOW)

    # The same plan day, so the same window: nothing is inserted and nothing is returned.
    assert EtaCalibration().fit(NOW + timedelta(hours=3)) is None
    assert EtaModelFit.query.count() == 1

    # A run that made its check before the first one committed (its check says no) is stopped by the unique window.
    checks = []
    already_fitted = calibration._already_fitted

    def check_made_too_early(data_to):
        checks.append(data_to)
        return False if len(checks) == 1 else already_fitted(data_to)

    with monkeypatch.context() as patch:
        patch.setattr(calibration, "_already_fitted", check_made_too_early)
        assert EtaCalibration().fit(NOW + timedelta(hours=3)) is None
    assert len(checks) == 2  # the early one, and the look at who won after the insert was refused
    assert EtaModelFit.query.count() == 1

    # The next Monday is another window: a new row.
    second = EtaCalibration().fit(NOW + timedelta(days=7))
    assert second is not None and second.id != first.id
    assert ensure_utc(second.data_to) - ensure_utc(first.data_to) == timedelta(days=7)
    assert EtaModelFit.query.count() == 2


def test_the_fit_beat_runs_monday_after_the_plan_day_closes(db, celery_app_module):
    celery = celery_app_module.celery
    entry = celery.conf.beat_schedule["fit-eta-model"]

    assert entry == {
        "task": "business_app.tasks.delivery_eta_tasks.fit_eta_model",
        "schedule": crontab(hour=5, minute=30, day_of_week=1),
        "options": {"time_limit": 600},
    }
    # Celery numbers Sunday 0, so 1 is Monday; its timezone is the display one, so this is Monday 05:30 in Tashkent.
    assert MONDAY.weekday() == 0
    assert entry["schedule"].day_of_week == {1}
    (hour,), (minute,) = entry["schedule"].hour, entry["schedule"].minute
    assert (hour, minute) == (WORK_DAY_START_HOUR, 30)

    # It lands after the plan day that ended at 05:00 closed, and the fit reads up to that boundary and no further.
    runs_at = local_instant_at_minutes(MONDAY, hour * 60 + minute)
    fit = EtaCalibration().fit(runs_at)
    assert ensure_utc(fit.data_to) == work_day_bounds(MONDAY)[0] < runs_at
    # The task is registered, and routed to the delivery queue with the other ETA tasks.
    assert entry["task"] in celery.tasks
    assert celery.amqp.router.route({}, entry["task"])["queue"].name == "delivery"


def test_the_weekly_task_honours_the_kill_switch_and_fits_once_a_week(app, db, monkeypatch):
    monkeypatch.setitem(app.config, "ETA_SHADOW_ENGINE_ENABLED", False)

    assert delivery_eta_tasks.fit_eta_model() == {"skipped": "disabled"}
    assert EtaModelFit.query.count() == 0

    monkeypatch.setitem(app.config, "ETA_SHADOW_ENGINE_ENABLED", True)
    monkeypatch.setattr(delivery_eta_tasks, "get_utc_now", lambda: NOW)

    summary = delivery_eta_tasks.fit_eta_model()
    assert summary["fit_id"] == only_fit().id
    assert summary["data_to"] == only_fit().data_to.isoformat()
    assert summary["kept_previous"] == only_fit().kept_previous
    assert delivery_eta_tasks.fit_eta_model() == {"skipped": "already_fitted"}
    assert EtaModelFit.query.count() == 1


# ---------------------------------------------------------------------------------------------------------------------
# R14, and the taps the fit leaves out.
# ---------------------------------------------------------------------------------------------------------------------


def test_the_actual_arrival_is_the_own_drivers_first_real_delivered_move(db):
    driver = make_driver(db)
    day = plan_day(3)
    plain, resaved, closed, colleague, redone = worked(
        db,
        driver,
        day,
        [
            Drop(2.0, 600),
            Drop(2.5, 620, reassigned=630),  # a re-save of the assignment is not a delivery
            Drop(3.0, 640, by=None),  # closed from the Orders screen
            Drop(3.5, 660, by=SOMEONE_ELSE),
            Drop(4.0, 680),
        ],
    )
    # The driver's first delivered tap stands even when the stop is taken back and delivered again.
    tap(db, redone.delivery, DeliveryStatus.IN_TRANSIT, local_instant_at_minutes(day, 690))
    tap(db, redone.delivery, DeliveryStatus.DELIVERED, local_instant_at_minutes(day, 700))
    waiting = order_at_km(db, 1.0, created_at=NOW - timedelta(days=40), driver=driver)
    ids = [order.delivery.id for order in (plain, resaved, closed, colleague, redone, waiting)]

    assert actual_arrivals(ids) == {
        plain.delivery.id: local_instant_at_minutes(day, 600),
        resaved.delivery.id: local_instant_at_minutes(day, 620),
        redone.delivery.id: local_instant_at_minutes(day, 680),
    }
    assert actual_arrivals([]) == {}


# One case per tap the fit must leave out: the drops of one driver's day, and what the fit makes of them -- the legs
# and depot pairs it used, and the rows left out by reason.
UNRELIABLE = [
    pytest.param([Drop(2.0, 600), Drop(2.5, 620), Drop(3.0, 640), Drop(3.5, 660)], 3, 1, {}, id="a clean day"),
    pytest.param(
        [Drop(2.0, 600), Drop(12.0, 605), Drop(12.5, 625)],
        1,
        1,
        {"too_fast": 1},
        id="a leg of a kilometre or more faster than 60 km/h",
    ),
    pytest.param(
        [Drop(2.0, 600), Drop(2.9, 600.5)], 1, 1, {}, id="a leg under a kilometre is never too fast, however quick"
    ),
    pytest.param(
        [Drop(2.0, 600), Drop(3.5, 600)],
        0,
        1,
        {"too_fast": 1},
        id="two stops tapped at once, a kilometre or more apart",
    ),
    pytest.param([Drop(10.0, 545)], 0, 0, {"too_fast": 1}, id="a first delivery from the depot faster than 60 km/h"),
    pytest.param(
        [Drop(2.0, 600), Drop(2.5, 620, picked_up=619), Drop(3.0, 640)],
        0,
        1,
        {"short_handover": 1},
        id="a stop picked up and delivered a minute apart: its legs are left out, not bridged",
    ),
    pytest.param(
        [Drop(2.0, 600), Drop(2.5, 620, picked_up=618), Drop(3.0, 640)],
        2,
        1,
        {},
        id="two minutes apart is not under two minutes",
    ),
    pytest.param(
        [Drop(2.0, 541, picked_up=None), Drop(2.5, 600)],
        1,
        0,
        {"short_handover": 1},
        id="a first delivery within two minutes of the day's first pick-up",
    ),
    pytest.param(
        [Drop(2.0, 600, picked_up=700)],
        0,
        0,
        {"pickup_after_delivery": 1},
        id="the day's first pick-up tapped after its first delivery gives no depot pair, and says so",
    ),
    pytest.param(
        [Drop(2.0, 541), Drop(2.5, 600)],
        0,
        0,
        {"short_handover": 1},
        id="a first stop picked up and delivered a minute apart gives no depot pair",
    ),
    pytest.param(
        [Drop(2.0, 600), Drop(2.5, 620, by=None), Drop(3.0, 640)],
        0,
        1,
        {"no_actor": 1},
        id="a delivered row with no actor breaks the chain: no leg into it, out of it or across it",
    ),
    pytest.param(
        [Drop(2.0, 600), Drop(2.5, 620, by=None), Drop(3.0, 640), Drop(3.5, 660)],
        1,
        1,
        {"no_actor": 1},
        id="only the legs around it are cut: the two stops after it still make a leg",
    ),
    pytest.param(
        [Drop(2.0, 600, by=None), Drop(2.5, 620), Drop(3.0, 640)],
        1,
        0,
        {"no_actor": 1},
        id="a day that opens with such a row gives no depot pair, not one that spans it",
    ),
    pytest.param(
        [Drop(2.0, 600), Drop(2.5, 620, by=SOMEONE_ELSE), Drop(3.0, 640)],
        0,
        1,
        {"not_the_drivers_tap": 1},
        id="a delivered row by someone other than the delivery's driver breaks the chain too",
    ),
    pytest.param(
        [Drop(2.0, 600, reassigned=610), Drop(2.5, 620)],
        1,
        1,
        {"reassign": 1},
        id="a reassign row (delivered to delivered) is no second stop",
    ),
    pytest.param(
        [Drop(2.0, 600), Drop(2.5, 620, door=False), Drop(3.0, 640)],
        0,
        1,
        {"no_point": 2},
        id="a stop with no coordinates has no distance",
    ),
]


@pytest.mark.parametrize("drops, legs, pairs, excluded", UNRELIABLE)
def test_the_fit_leaves_out_each_unreliable_tap_kind(db, drops, legs, pairs, excluded):
    worked(db, make_driver(db), plan_day(5), drops, loaded=540)

    fit = EtaCalibration().fit(NOW)

    assert (fit.rows_used["cycle"], fit.rows_used["depot"]) == (legs, pairs)
    assert nonzero(fit.rows_used["excluded"]) == excluded
    assert set(fit.rows_used["excluded"]) == set(EXCLUDED_REASONS)


def test_a_delivery_nobody_holds_is_counted_but_is_in_no_drivers_day(db):
    """Staff-entered orders are often closed from the Orders screen with no driver at all. Such a delivery is counted,
    but it is nobody's stop: it breaks no driver's chain, even when it was closed between two of theirs."""
    driver = make_driver(db)
    worked(db, driver, plan_day(5), [Drop(2.0, 600), Drop(2.5, 620)])
    closed = order_at_km(db, 3.0, created_at=local_instant_at_minutes(plan_day(5), 480))  # in the pool: no driver
    tap(db, closed.delivery, DeliveryStatus.DELIVERED, local_instant_at_minutes(plan_day(5), 610), by=None)

    fit = EtaCalibration().fit(NOW)

    assert fit.rows_used["cycle"] == 1
    assert nonzero(fit.rows_used["excluded"]) == {"no_actor": 1}


# A driver's day of four stops, delivered at 10:00, 10:20, 11:00 and 11:20 (the legs: 600-620, 620-660 and 660-680), and
# the loads that happen in it as (whose, minutes past midnight, bottles loaded) -> the legs fitted, the legs left out.
RELOADS = [
    pytest.param([("driver", 640, 30)], 2, 1, id="a load between two stops cuts that leg, and only that one"),
    pytest.param([("driver", 620, 30)], 3, 0, id="a load at the moment of a tap is not between its two legs"),
    pytest.param([("driver", 600, 30)], 3, 0, id="nor at the moment of the first tap"),
    pytest.param([("driver", 590, 30)], 3, 0, id="the load of the morning is before the first stop"),
    pytest.param([("driver", 640, 0)], 3, 0, id="a session opened by a hand-over loaded nothing"),
    pytest.param([("colleague", 640, 30)], 3, 0, id="another driver's load is not this driver's reload"),
    pytest.param([("driver", 610, 30), ("driver", 640, 30)], 1, 2, id="two loads in two legs cut both"),
    pytest.param([("driver", 630, 30), ("driver", 650, 30)], 2, 1, id="two loads in one leg cut it once"),
]


@pytest.mark.parametrize("loads, legs, reloads", RELOADS)
def test_a_leg_with_a_load_of_the_driver_inside_is_left_out(db, loads, legs, reloads):
    driver, colleague = make_driver(db), make_driver(db)
    day = plan_day(5)
    worked(db, driver, day, [Drop(2.0, 600), Drop(2.5, 620), Drop(3.0, 660), Drop(3.5, 680)], loaded=540)
    # A colleague works the same day, with a stop of their own between the driver's: their loads are read as well.
    worked(db, colleague, day, [Drop(4.0, 650)], loaded=540)
    for whose, minutes, bottles in loads:
        session = open_session(
            db, driver if whose == "driver" else colleague, local_instant_at_minutes(day, minutes), loaded=bottles
        )
        close_session(db, session, local_instant_at_minutes(day, minutes + 1))  # so the next load can open one

    fit = EtaCalibration().fit(NOW)

    assert fit.rows_used["cycle"] == legs
    assert nonzero(fit.rows_used["excluded"]) == ({"reload": reloads} if reloads else {})


# ---------------------------------------------------------------------------------------------------------------------
# The stop curve (R5, R13).
# ---------------------------------------------------------------------------------------------------------------------


def test_the_bins_are_the_edges_of_r5():
    km = (0.0, 0.19, 0.2, 0.99, 1.0, 2.9, 3.0, 5.99, 6.0, 9.99, 10.0, 30.0)

    assert [bin_index(x) for x in km] == [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5]


def test_a_thin_bin_keeps_the_previous_point(db):
    add_fit(db, MONDAY - timedelta(days=7), points=PREVIOUS_POINTS)
    driver = make_driver(db)
    # Bin 2 (1 to 3 km) has 29 legs: one short. Bin 3 (3 to 6 km) has exactly 30.
    worked(db, driver, plan_day(2), alternating(1.0, 3.0, 30, first=660, every=25))
    worked(db, driver, plan_day(3), alternating(1.0, 5.0, 31, first=360, every=35), loaded=330)

    fit = EtaCalibration().fit(NOW)

    points = fit.params["cycle_points"]
    assert points[3] == pytest.approx([4.0, 35.0])
    assert [points[n] for n in (0, 1, 2, 4, 5)] == [list(PREVIOUS_POINTS[n]) for n in (0, 1, 2, 4, 5)]
    assert kept_bins(fit) == {"bin:0", "bin:1", "bin:2", "bin:4", "bin:5"}
    assert fit.kept_previous["bin:2"].startswith("thin") and "29 legs" in fit.kept_previous["bin:2"]
    assert fit.kept_previous["bin:0"].startswith("thin") and "0 legs" in fit.kept_previous["bin:0"]
    assert fit.rows_used["cycle"] == 59


def lower_the_bin_minimum(app, monkeypatch, legs=3):
    """The shape checks are about the fitted curve, not the 30-leg minimum: here three legs fit a bin."""
    monkeypatch.setitem(app.config, "ETA_FIT_MIN_LEGS_PER_BIN", legs)


def test_a_curve_that_falls_with_distance_is_rejected(app, db, monkeypatch):
    lower_the_bin_minimum(app, monkeypatch)
    driver = make_driver(db)
    # 2 km legs of 40 minutes, then 4 km legs of 30: the farther bin is quicker than the nearer.
    worked(db, driver, plan_day(2), alternating(1.0, 3.0, 4, first=660, every=40))
    worked(db, driver, plan_day(3), alternating(1.0, 5.0, 4, first=660, every=30))

    fit = EtaCalibration().fit(NOW)

    assert fit.params["cycle_points"] == [list(point) for point in SEEDS]
    assert fit.kept_previous["cycle"].startswith("minutes fall with distance")
    # Both bins had enough legs: it is the curve that was refused, not those bins that were thin.
    assert kept_bins(fit) == {"bin:0", "bin:1", "bin:4", "bin:5"}
    assert fit.rows_used["cycle"] == 6


@pytest.mark.parametrize(
    "last_minutes, accepted",
    [
        pytest.param(53.0, False, id="3 minutes over 4 km is 80 km/h"),
        pytest.param(56.0, True, id="6 minutes over 4 km is 40 km/h"),
    ],
)
def test_a_curve_whose_last_segment_is_faster_than_sixty_kmh_is_rejected(app, db, monkeypatch, last_minutes, accepted):
    lower_the_bin_minimum(app, monkeypatch)
    driver = make_driver(db)
    # 8 km legs of 50 minutes (bin 4), then 12 km legs (bin 5): the curve keeps rising either way.
    worked(db, driver, plan_day(2), alternating(1.0, 9.0, 4, first=420, every=50))
    worked(db, driver, plan_day(3), alternating(0.5, 12.5, 4, first=420, every=last_minutes))

    fit = EtaCalibration().fit(NOW)

    points = fit.params["cycle_points"]
    if accepted:
        assert points[4] == pytest.approx([8.0, 50.0])
        assert points[5] == pytest.approx([12.0, last_minutes])
        assert "cycle" not in fit.kept_previous
    else:
        assert points == [list(point) for point in SEEDS]
        assert fit.kept_previous["cycle"].startswith("last segment faster than 60 km/h")


@pytest.mark.parametrize(
    "last_two",
    [
        pytest.param(((7.7, 44.0), (7.7, 67.0)), id="no gap in km between the last two points"),
        pytest.param(((13.2, 44.0), (7.7, 67.0)), id="the last point is nearer than the one before"),
    ],
)
def test_a_curve_whose_last_two_points_have_no_forward_distance_is_rejected_not_divided_by(db, last_two):
    """A zero gap would divide by zero and abort the weekly job; a negative one is not 'faster than 60 km/h'. The
    assembled curve (here the previous fit's, as no bin has legs) is refused with its own reason, and the previous
    curve stands."""
    points = PREVIOUS_POINTS[:4] + last_two
    add_fit(db, MONDAY - timedelta(days=7), points=points)

    fit = EtaCalibration().fit(NOW)

    assert fit.params["cycle_points"] == [list(point) for point in points]
    assert fit.kept_previous["cycle"].startswith("last two points do not move forward in distance")


# ---------------------------------------------------------------------------------------------------------------------
# The depot line (R4, R13).
# ---------------------------------------------------------------------------------------------------------------------


def pairs_on(a, b, count, *, km=None):
    """`count` (km, minutes) pairs on the line `a + b x km`, the n-th 2.0 + 0.75 n km out. With `km`, every pair is
    that far out instead, and takes `a + n` minutes."""
    if km is not None:
        return [(km, a + n) for n in range(count)]
    return [(x, a + b * x) for x in (2.0 + 0.75 * n for n in range(count))]


@pytest.mark.parametrize(
    "pairs, fitted, reason",
    [
        pytest.param(pairs_on(12.0, 2.0, 14), None, "thin: 14 pairs", id="14 pairs are one short"),
        pytest.param(pairs_on(12.0, 2.0, 15), (12.0, 2.0), None, id="15 pairs fit a line"),
        pytest.param(pairs_on(-5.0, 4.0, 15), None, "implausible", id="a negative intercept"),
        pytest.param(pairs_on(60.0, -2.0, 15), None, "implausible", id="a negative slope"),
        pytest.param(pairs_on(20.0, 0.0, 15, km=5.0), None, "pairs all at one distance", id="no spread to fit"),
    ],
)
def test_the_depot_line_needs_fifteen_pairs_and_non_negative_terms(db, pairs, fitted, reason):
    add_fit(db, MONDAY - timedelta(days=7), line=(11.0, 1.6))
    driver = make_driver(db)
    for n, (km, minutes) in enumerate(pairs):
        worked(db, driver, plan_day(n), [Drop(km, 540 + minutes)], loaded=540)

    fit = EtaCalibration().fit(NOW)

    assert fit.rows_used["depot"] == len(pairs)
    if fitted is not None:
        assert fit.params["depot_line"] == pytest.approx(list(fitted))
        assert "depot" not in fit.kept_previous
    else:
        # The previous values are the last fit's (11.0, 1.6), not the seeds.
        assert fit.params["depot_line"] == [11.0, 1.6]
        assert fit.kept_previous["depot"].startswith(reason)


# ---------------------------------------------------------------------------------------------------------------------
# The window buckets (R11, R13, R14).
# ---------------------------------------------------------------------------------------------------------------------


def test_a_window_bucket_under_thirty_stores_only_its_n(db):
    driver = make_driver(db)
    (order,) = worked(db, driver, plan_day(10), [Drop(2.0, 600)])
    arrival = local_instant_at_minutes(plan_day(10), 600)
    # 30 promises for a stop with 1-2 ahead on a car that has started, from 10 minutes early to 19 late...
    late_by(db, order.delivery, arrival, range(-10, 20), stops_ahead=2, started=True)
    # ...and 29 for the first stop of a car that has not.
    late_by(db, order.delivery, arrival, range(29), stops_ahead=0, started=False)

    fit = EtaCalibration().fit(NOW)

    # The 10th and 90th percentiles of (arrival - expected): ranks 2.9 and 26.1 of -10..19, rounded to the minute.
    assert fit.params["window_buckets"] == {
        bucket_key(2, True): {"lo": -7, "hi": 16, "n": 30},
        bucket_key(0, False): {"lo": None, "hi": None, "n": 29},
    }
    assert fit.rows_used["window"] == 59
    # A thin bucket is not a component the fit kept: it borrows when it is read (R11), so nothing records it as kept.
    assert not any("window" in key or "bucket" in key for key in fit.kept_previous)


def test_only_engine_rows_in_the_window_with_the_drivers_arrival_become_samples(db):
    data_from, data_to = work_day_bounds(plan_day(0))[0], work_day_bounds(MONDAY)[0]
    driver = make_driver(db)
    delivered, closed_by_nobody = worked(db, driver, plan_day(10), [Drop(2.0, 600), Drop(2.5, 620, by=None)])
    never = order_at_km(db, 1.0, created_at=NOW - timedelta(days=3), driver=driver)
    # Delivered at 06:00 on the Monday the fit runs: after the window, but known by the time the fit runs.
    (after_the_window,) = worked(db, driver, MONDAY, [Drop(2.0, 6 * 60)], loaded=5 * 60 + 10)
    expected = local_instant_at_minutes(plan_day(10), 600) - timedelta(minutes=5)

    promise(db, delivered.delivery, expected, stops_ahead=1, started=True)  # a sample
    promise(db, delivered.delivery, expected, stops_ahead=0, started=False, computed_at=data_from)  # the first instant
    promise(db, delivered.delivery, expected, computed_at=data_from - timedelta(seconds=1))  # before the window
    promise(db, delivered.delivery, expected, computed_at=data_to)  # the window is half open
    promise(db, delivered.delivery, None)  # unconfirmed: no expected time
    promise(db, delivered.delivery, expected, model_version=MODEL_GOOGLE_TAP, stage=ETA_STAGE_ON_THE_WAY, shown=True)
    promise(db, delivered.delivery, expected, stage=ETA_STAGE_ON_THE_WAY)
    promise(db, closed_by_nobody.delivery, expected)  # no driver arrival
    promise(db, never.delivery, expected)  # not delivered at all
    promise(db, after_the_window.delivery, NOW - timedelta(minutes=30), stops_ahead=3)  # a sample: its arrival is known

    fit = EtaCalibration().fit(NOW)

    assert fit.params["window_buckets"] == {
        bucket_key(1, True): {"lo": None, "hi": None, "n": 1},
        bucket_key(0, False): {"lo": None, "hi": None, "n": 1},
        bucket_key(3, True): {"lo": None, "hi": None, "n": 1},
    }
    assert fit.rows_used["window"] == 3


# ---------------------------------------------------------------------------------------------------------------------
# Monitoring (spec 4.5): the ratio of the time a stop took to the curve, by time of day and by size.
# ---------------------------------------------------------------------------------------------------------------------

# Legs of 2.3 km, where the seed curve says 24 minutes: a leg of `ratio` x 24 minutes has that ratio against the curve.
LEG_MINUTES = cycle_minutes(2.3, SEEDS)


def ratio_chain(first: float, ratios, bottles) -> List[Drop]:
    """A chain of stops from `first` (minutes past midnight) whose n-th leg takes `ratios[n]` x the curve and ends at a
    stop of `bottles[n]` bottles."""
    drops = [Drop(1.0, first)]
    for n, (ratio, size) in enumerate(zip(ratios, bottles)):
        drops.append(Drop(3.3 if n % 2 == 0 else 1.0, drops[-1].at + ratio * LEG_MINUTES, bottles=size))
    return drops


def test_the_fit_reports_the_time_of_day_and_size_ratios(db):
    driver = make_driver(db)
    slow = 25 / LEG_MINUTES  # a leg of 25 minutes against the curve's 24
    worked(db, driver, plan_day(1), ratio_chain(13 * 60 + 15, [1.1, 1.2, 1.3], [2, 3, 4]), loaded=600)  # 13:41-14:41
    day_two = ratio_chain(19 * 60 + 15, [0.9, 1.0], [8, 8])  # delivered 19:36 and 20:00
    # A third leg, 11 km away in two minutes: too fast to be a stop, so it is in no ratio.
    day_two.append(Drop(12.5, day_two[-1].at + 2, bottles=25))
    worked(db, driver, plan_day(2), day_two, loaded=600)
    worked(db, driver, plan_day(3), ratio_chain(23 * 60 + 5, [slow], [15]), loaded=600)  # 23:30: the last band
    worked(db, driver, plan_day(4), ratio_chain(10 * 60 + 5, [slow], [30]), loaded=600)  # 10:30: before 11:00
    worked(db, driver, plan_day(5), ratio_chain(24 * 60 + 5, [slow], [45]), loaded=600)  # 00:30: after midnight

    fit = EtaCalibration().fit(NOW)

    by_hour, by_size = fit.params["monitoring"]["by_hour_band"], fit.params["monitoring"]["by_bottles_band"]
    # A leg is in the two-hour band of the time it was delivered; the middle of 1.1, 1.2 and 1.3 is 1.2.
    assert set(by_hour) == {"13-15", "19-21", "23-24"}
    assert by_hour["13-15"] == {"ratio": pytest.approx(1.2), "n": 3}
    assert by_hour["19-21"] == {"ratio": pytest.approx(0.95), "n": 2}
    assert by_hour["23-24"] == {"ratio": pytest.approx(slow), "n": 1}
    # ...and in the band of the bottles of the order it ended at; 45 bottles are in none.
    assert set(by_size) == {"0-5", "6-10", "11-20", "21-40"}
    assert by_size["0-5"] == {"ratio": pytest.approx(1.2), "n": 3}
    assert by_size["6-10"] == {"ratio": pytest.approx(0.95), "n": 2}
    assert by_size["11-20"] == {"ratio": pytest.approx(slow), "n": 1}
    assert by_size["21-40"] == {"ratio": pytest.approx(slow), "n": 1}
    assert fit.rows_used["cycle"] == 8
    assert nonzero(fit.rows_used["excluded"]) == {"too_fast": 1}


def one_leg_ending(db, delivered_at: float, bottles: int = 1) -> None:
    """A single 25-minute leg of 2.3 km, delivered at `delivered_at` minutes past a plan day's midnight, to an order of
    `bottles` bottles."""
    drops = [Drop(1.0, delivered_at - 25), Drop(3.3, delivered_at, bottles=bottles)]
    worked(db, make_driver(db), plan_day(1), drops, loaded=delivered_at - 120)


@pytest.mark.parametrize(
    "delivered_at, band",
    [
        pytest.param(10 * 60 + 59, None, id="10:59 is before the bands"),
        pytest.param(11 * 60, "11-13", id="11:00 opens the first band"),
        pytest.param(12 * 60 + 59, "11-13", id="12:59"),
        pytest.param(13 * 60, "13-15", id="13:00"),
        pytest.param(22 * 60 + 59, "21-23", id="22:59"),
        pytest.param(23 * 60, "23-24", id="23:00 opens the last band, which is one hour"),
        pytest.param(23 * 60 + 59, "23-24", id="23:59"),
        pytest.param(24 * 60, None, id="midnight is after the bands"),
    ],
)
def test_the_time_of_day_bands_run_from_11_to_midnight_in_local_time(db, delivered_at, band):
    one_leg_ending(db, delivered_at)

    by_hour = EtaCalibration().fit(NOW).params["monitoring"]["by_hour_band"]

    assert set(by_hour) == ({band} if band else set())


@pytest.mark.parametrize(
    "bottles, band",
    [
        pytest.param(0, "0-5", id="0"),
        pytest.param(5, "0-5", id="5"),
        pytest.param(6, "6-10", id="6"),
        pytest.param(10, "6-10", id="10"),
        pytest.param(11, "11-20", id="11"),
        pytest.param(20, "11-20", id="20"),
        pytest.param(21, "21-40", id="21"),
        pytest.param(40, "21-40", id="40"),
        pytest.param(41, None, id="41 is above the bands"),
    ],
)
def test_the_size_bands_are_the_bottles_of_the_order_the_leg_ended_at(db, bottles, band):
    one_leg_ending(db, 14 * 60, bottles)

    by_size = EtaCalibration().fit(NOW).params["monitoring"]["by_bottles_band"]

    assert set(by_size) == ({band} if band else set())


# ---------------------------------------------------------------------------------------------------------------------
# One shape, written and read back (the Task 4 review).
# ---------------------------------------------------------------------------------------------------------------------


def test_a_fit_reads_back_through_current_params_and_lo_hi(app, db):
    """The fit writes the row and `current_params` / `lo_hi` read it, both production code, against values the test
    knows: a key renamed on one side only breaks this test, where two hand-built fixtures that agree would not."""
    driver = make_driver(db)
    first_orders = []
    for n in range(15):
        x = 2.0 + 0.5 * n
        # Loaded at 11:00. The first delivery is 12 + 2 x minutes later (the depot line); the next two are 4 km legs of
        # 35 minutes (bin 3). All are delivered before 13:00.
        first = 11 * 60 + 12 + 2 * x
        # The third is picked up later, at 11:40: the pair still starts at the day's first pick-up, at 11:00.
        drops = [Drop(x, first), Drop(x + 4.0, first + 35), Drop(x, first + 70, picked_up=11 * 60 + 40)]
        first_orders.append(worked(db, driver, plan_day(n), drops, loaded=11 * 60)[0])
    first_arrival = local_instant_at_minutes(plan_day(0), 11 * 60 + 12 + 2 * 2.0)
    # 30 promises that were 4k - 15 minutes late (-11 to 105), for a stop with 1-2 ahead on a car that had started.
    late_by(db, first_orders[0].delivery, first_arrival, [4 * k - 15 for k in range(1, 31)], stops_ahead=1)

    fit = EtaCalibration().fit(NOW)
    params = current_params(NOW + timedelta(minutes=1))

    assert params.source == f"fit:{fit.id}"
    assert (fit.rows_used["cycle"], fit.rows_used["depot"], fit.rows_used["window"]) == (30, 15, 30)
    # The depot line is the least-squares line through the 15 pairs: exactly 12 + 2 x km.
    assert params.depot_line == pytest.approx((12.0, 2.0))
    # Bin 3 is fitted, to the middle of 30 legs of 4 km and 35 minutes. The other five points are the seeds: no other
    # bin had a leg.
    assert params.cycle_points[3] == pytest.approx((4.0, 35.0))
    assert params.cycle_points[:3] + params.cycle_points[4:] == SEEDS[:3] + SEEDS[4:]
    assert cycle_minutes(params.cycle_points[3][0], params.cycle_points) == pytest.approx(35.0)
    # The bucket with 30 promises is read as fitted: its 10th and 90th percentiles are ranks 2.9 and 26.1 of -11..105.
    # hi is lifted to the lower band's, which is the seed's 45, so the fitted 93 stands.
    assert lo_hi(params, 1, True) == (1, 93, False)
    assert lo_hi(params, 2, True) == (1, 93, False)
    # A bucket the fit never saw reads its seed, with the extra hour on hi for a car that has not started.
    assert lo_hi(params, 0, False) == (-20, 45 + app.config["ETA_NOT_STARTED_EXTRA_HI_MINUTES"], True)
    # The monitoring is measured against the curve the row holds: 35 minutes over a 35-minute point is a ratio of 1.
    by_hour = fit.params["monitoring"]["by_hour_band"]
    assert set(by_hour) == {"11-13"} and by_hour["11-13"]["n"] == 30
    assert by_hour["11-13"]["ratio"] == pytest.approx(1.0)
    assert fit.params["monitoring"]["by_bottles_band"]["0-5"]["ratio"] == pytest.approx(1.0)


def test_with_no_history_every_component_keeps_the_seeds_and_says_why(db):
    fit = EtaCalibration().fit(NOW)

    assert fit.params["cycle_points"] == [list(point) for point in SEEDS]
    assert fit.params["depot_line"] == list(SEED_LINE)
    assert fit.params["window_buckets"] == {}
    assert fit.params["monitoring"] == {"by_hour_band": {}, "by_bottles_band": {}}
    assert fit.rows_used == {"cycle": 0, "depot": 0, "window": 0, "excluded": dict.fromkeys(EXCLUDED_REASONS, 0)}
    assert set(fit.kept_previous) == {f"bin:{n}" for n in range(6)} | {"depot"}
    assert current_params(NOW + timedelta(minutes=1)).source == f"fit:{fit.id}"
