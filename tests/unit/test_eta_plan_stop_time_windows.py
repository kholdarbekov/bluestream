"""Stop time, parameters and windows of the capacity-aware ETA plan (honest-ETA Phase 2, Task 4): the pure pieces the
planner and the recorder share.

- The stop curve and the depot line (R4, R5): the minutes of a leg from the straight-line km between its ends.
- The window rules (R11): bucket keys and borrowing, rounding outward on the local clock, a customer's own window, the
  day label and the promised day.
- The parameter set (R13): `current_params` reads the latest weekly fit that exists at `now`, else the seeds. Its rows
  are real, in the suite's own database, in the shape `EtaCalibration` will write (the plan's Ruling P4).

Every instant is handed in on the Tashkent wall clock the owner reads (`at`), so no test depends on the time of day.
`TODAY` is Wednesday 2026-11-04.
"""

import dataclasses
from datetime import date, datetime, time, timedelta

import pytest

from business_app.models.delivery_promise import PROMISE_DAY_KINDS
from business_app.services.eta_plan.params import EtaParams, current_params
from business_app.services.eta_plan.stop_time import cycle_minutes, depot_leg_minutes
from business_app.services.eta_plan.windows import (
    STOPS_AHEAD_BANDS,
    band_of,
    bucket_key,
    customer_bounded_window,
    day_kind,
    display_window,
    lo_hi,
    promised_date,
)
from business_app.utils.delivery_window import window_edge_utc
from shared import business_config
from tests.integration.eta_plan_builders import add_fit, written_on

pytestmark = pytest.mark.unit

TODAY = date(2026, 11, 4)
TOMORROW = TODAY + timedelta(days=1)
SEED_POINTS = business_config.ETA_CYCLE_SEED_POINTS
SEED_LINE = business_config.ETA_DEPOT_LEG_SEED
# A bucket the fit stored thin: its n, and no lo or hi (R13).
THIN = {"lo": None, "hi": None, "n": 12}


def at(hour: int, minute: int = 0, *, day: date = TODAY) -> datetime:
    """`hour:minute` on the Tashkent wall clock on the local date `day`, as the aware UTC instant the plan works in."""
    return window_edge_utc(day, time(hour, minute))


def bucket(lo, hi, n):
    return {"lo": lo, "hi": hi, "n": n}


def params_with(buckets=None) -> EtaParams:
    """A fitted parameter set holding exactly these window buckets, over the seed curve and line."""
    return EtaParams(cycle_points=SEED_POINTS, depot_line=SEED_LINE, window_buckets=buckets or {}, source="fit:7")


class TestTheStopCurve:
    def test_the_seed_curve_passes_through_the_spec_medians(self):
        assert cycle_minutes(0.8, SEED_POINTS) == pytest.approx(16)
        # Halfway between (0.8, 16) and (2.3, 24).
        assert cycle_minutes(1.55, SEED_POINTS) == pytest.approx(20.0)

    @pytest.mark.parametrize("km, minutes", SEED_POINTS)
    def test_the_curve_passes_through_every_point(self, km, minutes):
        assert cycle_minutes(km, SEED_POINTS) == pytest.approx(minutes)

    def test_the_curve_is_flat_below_the_first_point_and_extends_the_last_slope(self):
        assert cycle_minutes(0.0, SEED_POINTS) == pytest.approx(13)
        assert cycle_minutes(0.05, SEED_POINTS) == pytest.approx(13)
        # The last segment, (7.7, 44) to (13.2, 67), climbs 23 / 5.5 minutes per km: 5.5 km past 13.2 km adds 23.
        assert cycle_minutes(18.7, SEED_POINTS) == pytest.approx(90.0)

    def test_the_curve_never_falls_as_the_distance_grows(self):
        minutes = [cycle_minutes(step / 20, SEED_POINTS) for step in range(0, 801)]  # 0 to 40 km in 50 m steps

        assert minutes == sorted(minutes)

    def test_the_curve_follows_the_points_it_is_given_not_the_seeds(self):
        points = ((1.0, 10.0), (3.0, 30.0))

        assert cycle_minutes(0.5, points) == pytest.approx(10.0)  # flat below the first point
        assert cycle_minutes(2.0, points) == pytest.approx(20.0)
        assert cycle_minutes(4.0, points) == pytest.approx(40.0)  # the last segment's 10 minutes per km, past its end
        # A stored fit comes back from JSON as lists, and the curve reads those just the same.
        assert cycle_minutes(4.0, [[1.0, 10.0], [3.0, 30.0]]) == pytest.approx(40.0)


class TestTheDepotLine:
    def test_the_depot_line_is_ten_plus_one_and_a_half_per_km(self):
        assert depot_leg_minutes(28.9, SEED_LINE) == pytest.approx(53.35)
        assert depot_leg_minutes(0.0, SEED_LINE) == pytest.approx(10.0)

    def test_the_depot_line_follows_the_line_it_is_given_not_the_seed(self):
        assert depot_leg_minutes(10.0, (5.0, 2.0)) == pytest.approx(25.0)
        assert depot_leg_minutes(10.0, [5.0, 2.0]) == pytest.approx(25.0)

    def test_at_the_far_depot_the_line_is_far_cheaper_than_the_curve(self):
        """Why R4 has a line at all: the curve, fitted to legs of 13.2 km or less, says about 133 minutes at the
        production depot's 28.9 km from the centre, where loading to the first delivery takes 33 to 119."""
        assert cycle_minutes(28.9, SEED_POINTS) == pytest.approx(132.65, abs=0.01)
        assert depot_leg_minutes(28.9, SEED_LINE) < cycle_minutes(28.9, SEED_POINTS) / 2


class TestBucketKeys:
    def test_bands_and_bucket_keys(self):
        assert STOPS_AHEAD_BANDS == ("0", "1-2", "3-4", "5-7", "8+")
        assert [band_of(ahead) for ahead in range(0, 10)] == [
            "0",
            "1-2",
            "1-2",
            "3-4",
            "3-4",
            "5-7",
            "5-7",
            "5-7",
            "8+",
            "8+",
        ]
        assert band_of(60) == "8+"
        assert bucket_key(0, False) == "0|not_started"
        assert bucket_key(2, True) == "1-2|started"
        assert bucket_key(7, False) == "5-7|not_started"
        assert bucket_key(9, True) == "8+|started"

    def test_every_band_has_a_seed_window(self):
        """The seed table is keyed by the very labels the buckets use, so a band never lacks a seed."""
        assert set(STOPS_AHEAD_BANDS) == set(business_config.ETA_WINDOW_SEEDS)


class TestLoAndHi:
    def test_with_no_fit_every_band_reads_its_seed(self):
        seeds = business_config.ETA_WINDOW_SEEDS

        for ahead, band in [(0, "0"), (2, "1-2"), (4, "3-4"), (6, "5-7"), (20, "8+")]:
            assert lo_hi(params_with(), ahead, True) == (*seeds[band], True), band

    def test_a_fitted_bucket_with_thirty_promises_is_used_as_it_is(self):
        params = params_with({"1-2|started": bucket(-33, 77, 30)})

        assert lo_hi(params, 1, True) == (-33, 77, False)

    def test_a_bucket_under_thirty_borrows_the_next_band_then_the_seed(self):
        # "0|started" is thin, so it reads "1-2|started".
        borrowed = params_with({"0|started": THIN, "1-2|started": bucket(-33, 77, 40)})
        assert lo_hi(borrowed, 0, True) == (-33, 77, False)

        # The borrowing cascades: every band up to the first fitted one is thin.
        cascaded = params_with({"0|started": THIN, "1-2|started": THIN, "3-4|started": bucket(-55, 110, 31)})
        assert lo_hi(cascaded, 0, True) == (-55, 110, False)
        assert lo_hi(cascaded, 2, True) == (-55, 110, False)

        # ...up to "8+" at most.
        to_the_end = params_with({"0|started": THIN, "5-7|started": THIN, "8+|started": bucket(-95, 190, 50)})
        assert lo_hi(to_the_end, 0, True) == (-95, 190, False)

        # With every started bucket thin there is nothing to borrow: the band's own seed.
        every_band_thin = params_with({f"{band}|started": THIN for band in STOPS_AHEAD_BANDS})
        assert lo_hi(every_band_thin, 1, True) == (-40, 90, True)
        assert lo_hi(every_band_thin, 3, True) == (-60, 120, True)

    def test_thirty_is_the_threshold_and_n_decides_not_the_presence_of_values(self):
        # A bucket that carries values but only 29 promises is thin: it borrows.
        params = params_with({"0|started": bucket(-5, 10, 29), "1-2|started": bucket(-33, 77, 30)})
        assert lo_hi(params, 0, True) == (-33, 77, False)

        # A bucket with enough promises but no values has nothing to give either.
        no_values = params_with({"0|started": bucket(None, None, 45)})
        assert lo_hi(no_values, 0, True) == (-20, 45, True)

    def test_a_started_bucket_never_borrows_from_the_not_started_ones(self):
        params = params_with({"0|not_started": bucket(-30, 70, 40), "1-2|not_started": bucket(-45, 95, 40)})

        assert lo_hi(params, 0, False) == (-30, 70, False)
        assert lo_hi(params, 0, True) == (-20, 45, True)

    def test_the_extra_hour_applies_to_seeded_not_started_windows_only(self):
        # Seeded: "0" reads -20 / +45, and a car that has not started gets an hour more on hi (spec 4.6).
        assert lo_hi(params_with(), 0, True) == (-20, 45, True)
        assert lo_hi(params_with(), 0, False) == (-20, 105, True)
        assert lo_hi(params_with(), 1, False) == (-40, 150, True)

        # Fitted: the fitted hi already holds that spread, so it is not added again.
        fitted = params_with({"0|not_started": bucket(-30, 70, 40)})
        assert lo_hi(fitted, 0, False) == (-30, 70, False)

    def test_hi_never_falls_as_more_stops_are_ahead(self):
        # "3-4" fitted a smaller hi than "1-2": it reads the "1-2" value. lo is not lifted.
        params = params_with({"1-2|started": bucket(-40, 100, 50), "3-4|started": bucket(-50, 80, 50)})

        assert lo_hi(params, 1, True) == (-40, 100, False)
        assert lo_hi(params, 3, True) == (-50, 100, False)

    def test_the_envelope_reaches_a_seeded_band_from_a_fitted_one_below_it(self):
        # "0" is fitted wide and "1-2" has no fit, so its seed hi (90) is lifted to the fitted band below it.
        params = params_with(
            {
                "0|started": bucket(-25, 130, 40),
                "1-2|started": THIN,
                "3-4|started": THIN,
                "5-7|started": THIN,
                "8+|started": THIN,
            }
        )

        assert lo_hi(params, 1, True) == (-40, 130, True)
        assert lo_hi(params, 3, True) == (-60, 130, True)
        assert lo_hi(params, 6, True) == (-75, 150, True)  # its own seed is already above the envelope

    @pytest.mark.parametrize("started", [True, False])
    @pytest.mark.parametrize(
        "buckets",
        [
            pytest.param({}, id="seeds"),
            pytest.param(
                {
                    "0|started": bucket(-10, 300, 60),
                    "1-2|started": bucket(-30, 90, 60),
                    "3-4|started": THIN,
                    "5-7|started": bucket(-60, 120, 35),
                    "8+|started": bucket(-80, 100, 90),
                    "0|not_started": bucket(-20, 40, 31),
                    "1-2|not_started": bucket(-30, 400, 31),
                    "5-7|not_started": bucket(-70, 60, 70),
                },
                id="fitted-and-not-monotone",
            ),
        ],
    )
    def test_hi_is_non_decreasing_over_every_band_for_any_fit(self, buckets, started):
        """The invariant a second car relies on (R9): a stop with more ahead never reads a smaller hi."""
        his = [lo_hi(params_with(buckets), ahead, started)[1] for ahead in range(0, 14)]

        assert his == sorted(his)


class TestTheDisplayWindow:
    def test_the_window_is_rounded_outward_and_never_starts_before_now(self):
        expected = at(14, 10)

        assert display_window(expected, -40, 90, at(11, 0)) == (at(13, 30), at(16, 0))
        # A window that has begun starts at now.
        assert display_window(expected, -40, 90, at(13, 45)) == (at(13, 45), at(16, 0))

    def test_a_raw_edge_off_the_half_hour_moves_outward(self):
        # 14:25 - 40 = 13:45 floors to 13:30; 14:25 + 90 = 15:55 ceils to 16:00.
        assert display_window(at(14, 25), -40, 90, at(11, 0)) == (at(13, 30), at(16, 0))
        # A few seconds past a boundary still ceils to the next one, and floors to the one before.
        expected = at(14, 10) + timedelta(seconds=20)
        assert display_window(expected, -40, 90, at(11, 0)) == (at(13, 30), at(16, 0))

    def test_a_past_window_end_is_pushed_after_its_start(self):
        start, end = display_window(at(14, 10), -140, -120, at(13, 45))

        assert start == at(13, 45)  # now: the raw window is already over
        assert end == at(14, 0)  # the next half hour after the start
        # Starting exactly on a half hour, the end is the one after that.
        assert display_window(at(14, 10), -140, -120, at(14, 0)) == (at(14, 0), at(14, 30))

    def test_the_window_rounds_on_the_local_clock_not_on_utc(self, app, monkeypatch):
        """Tashkent is five hours ahead of UTC, so a half-hour step lands on the same minutes either way. A 45-minute
        step does not: 14:10 local floors to 13:30 on the local clock, and to 14:00 on a UTC one."""
        monkeypatch.setitem(app.config, "ETA_WINDOW_ROUND_MINUTES", 45)

        # 14:10 + 0 floors to 13:30 (810 minutes past local midnight); 14:10 + 55 = 15:05 ceils to 15:45.
        assert display_window(at(14, 10), 0, 55, at(9, 0)) == (at(13, 30), at(15, 45))

    @pytest.mark.parametrize("lo, hi", [(-40, 90), (-20, 45), (-90, 180), (-30, -10), (10, 5), (0, 0), (-60, 400)])
    @pytest.mark.parametrize("now_minute", [0, 7, 30, 59])
    def test_the_displayed_window_always_holds_the_raw_one_and_never_inverts(self, lo, hi, now_minute):
        expected = at(14, 10) + timedelta(seconds=20)
        now = at(13, now_minute) + timedelta(seconds=11)

        start, end = display_window(expected, lo, hi, now)

        assert start >= now
        assert end > start  # the DB refuses window_start > window_end, and an empty window tells nobody anything
        assert start <= max(now, expected + timedelta(minutes=lo))  # rounded outward
        assert end >= expected + timedelta(minutes=hi)
        for edge in (start, end):
            if edge != now:  # every edge but a window starting right now sits on the local half hour
                assert (edge - at(0)).total_seconds() % 1800 == 0, edge  # at(0) is local midnight


class TestACustomersOwnWindow:
    def test_a_customer_window_wins_when_the_plan_meets_it(self):
        # Between 14:00 and 17:00, and the plan reaches the door at 15:00: the customer's own window.
        assert customer_bounded_window(at(15, 0), -40, 90, at(10, 0), at(14, 0), at(17, 0)) == (at(14, 0), at(17, 0))
        # Until 12:00, and the plan reaches the door at 11:00: from now to the customer's end.
        assert customer_bounded_window(at(11, 0), -40, 90, at(10, 0), None, at(12, 0)) == (at(10, 0), at(12, 0))

    def test_arriving_exactly_at_the_end_of_the_window_still_meets_it(self):
        assert customer_bounded_window(at(17, 0), -40, 90, at(10, 0), at(14, 0), at(17, 0)) == (at(14, 0), at(17, 0))
        assert customer_bounded_window(at(12, 0), -40, 90, at(10, 0), None, at(12, 0)) == (at(10, 0), at(12, 0))

    def test_a_stop_ahead_of_its_window_still_gets_the_customers_window(self):
        """The plan never puts a windowed stop before its window starts (the driver waits), but if one were early the
        customer's own window is still the honest answer, and never a computed one that begins before theirs."""
        assert customer_bounded_window(at(13, 0), -40, 90, at(10, 0), at(14, 0), at(17, 0)) == (at(14, 0), at(17, 0))

    def test_a_between_window_never_starts_before_now(self):
        assert customer_bounded_window(at(11, 0), -40, 90, at(10, 15), at(9, 0), at(12, 0)) == (at(10, 15), at(12, 0))

    def test_an_until_window_starts_at_the_plan_days_start_when_that_is_later_than_now(self):
        """The plan day opens at 05:00, not at midnight: at 02:00 a stop due by noon is promised from 05:00."""
        assert customer_bounded_window(at(11, 0), -40, 90, at(2, 0), None, at(12, 0)) == (at(5, 0), at(12, 0))
        # Tomorrow's stop is bounded by tomorrow's 05:00 whatever the hour now.
        tomorrow = customer_bounded_window(at(11, 0, day=TOMORROW), -40, 90, at(10, 0), None, at(12, 0, day=TOMORROW))
        assert tomorrow == (at(5, 0, day=TOMORROW), at(12, 0, day=TOMORROW))

    def test_an_after_window_never_starts_before_the_customers_time(self):
        """For an "after 18:00" window with the plan arriving at 18:00 and lo = -40, the raw start 17:20 floors to
        17:00, but the customer said after 18:00."""
        assert customer_bounded_window(at(18, 0), -40, 90, at(10, 15), at(18, 0), None) == (at(18, 0), at(19, 30))

    def test_an_after_window_starts_later_when_the_plan_does(self):
        # The plan reaches the door at 20:00, so the raw start 19:20 floors to 19:00, later than the customer's 18:00.
        assert customer_bounded_window(at(20, 0), -40, 90, at(10, 15), at(18, 0), None) == (at(19, 0), at(21, 30))
        # A window that has begun starts at now.
        assert customer_bounded_window(at(20, 0), -40, 90, at(19, 40), at(18, 0), None) == (at(19, 40), at(21, 30))

    def test_an_after_window_is_never_empty(self):
        # A fit whose plans run pessimistic can have a negative hi: the raw end (17:30) is before the customer's start
        # (18:00), so the end moves to the half hour after the start.
        assert customer_bounded_window(at(18, 0), -90, -30, at(10, 15), at(18, 0), None) == (at(18, 0), at(18, 30))

    def test_a_plan_after_the_customers_window_records_the_computed_window(self):
        late_between = customer_bounded_window(at(17, 40), -40, 90, at(10, 0), at(14, 0), at(17, 0))
        assert late_between == display_window(at(17, 40), -40, 90, at(10, 0)) == (at(17, 0), at(19, 30))

        late_until = customer_bounded_window(at(12, 20), -40, 90, at(10, 0), None, at(12, 0))
        assert late_until == display_window(at(12, 20), -40, 90, at(10, 0)) == (at(11, 30), at(14, 0))

    def test_without_a_customer_window_it_is_the_display_window(self):
        window = customer_bounded_window(at(14, 10), -40, 90, at(11, 0), None, None)

        assert window == display_window(at(14, 10), -40, 90, at(11, 0)) == (at(13, 30), at(16, 0))


class TestTheDayAndThePromisedDate:
    def test_day_kind_uses_calendar_dates(self):
        # At 02:00 the plan day is still yesterday's, but the customer's calendar says today: 16:00 is "today".
        assert day_kind(at(16, 0), at(2, 0)) == "today"
        assert day_kind(at(9, 0, day=TOMORROW), at(2, 0)) == "tomorrow"
        assert day_kind(at(9, 0, day=TODAY + timedelta(days=2)), at(2, 0)) == "date"

    @pytest.mark.parametrize(
        "expected, now, kind",
        [
            pytest.param(at(23, 59), at(0, 10), "today", id="the-last-minute-of-today"),
            pytest.param(at(0, 20, day=TOMORROW), at(23, 50), "tomorrow", id="half-an-hour-away-is-tomorrow"),
            pytest.param(at(0, 0, day=TOMORROW), at(0, 0), "tomorrow", id="local-midnight"),
            pytest.param(at(10, 0, day=TODAY + timedelta(days=7)), at(10, 0), "date", id="a-week-on"),
        ],
    )
    def test_day_kind_edges(self, expected, now, kind):
        assert day_kind(expected, now) == kind

    def test_day_kind_reads_the_local_date_not_the_utc_one(self):
        """01:30 on the 5th in Tashkent is 20:30 UTC on the 4th: tomorrow to a customer at 22:00 on the 4th."""
        assert day_kind(at(1, 30, day=TOMORROW), at(22, 0)) == "tomorrow"

    def test_day_kind_is_always_a_value_the_promise_check_accepts(self):
        now = at(2, 0)
        kinds = {day_kind(now + timedelta(hours=hour), now) for hour in range(0, 24 * 6)}

        assert kinds == {"today", "tomorrow", "date"}
        assert kinds <= set(PROMISE_DAY_KINDS)

    def test_the_promised_date_is_the_local_date_of_e(self):
        # E at 23:40 with a window that ends at 01:00 the next morning still promises today.
        expected = at(23, 40)
        _, window_end = display_window(expected, -20, 80, at(11, 0))
        assert window_end == at(1, 0, day=TOMORROW)
        assert promised_date(expected, None) == TODAY
        assert day_kind(expected, at(11, 0)) == "today"  # the label reads the very same instant

        # 01:30 on the 5th in Tashkent is the 4th in UTC: the promised date is the customer's, not UTC's.
        assert promised_date(at(1, 30, day=TOMORROW), None) == TOMORROW

    def test_a_dated_row_without_a_time_promises_the_customers_date(self):
        assert promised_date(None, TODAY + timedelta(days=5)) == TODAY + timedelta(days=5)

    def test_an_unconfirmed_row_promises_no_date(self):
        assert promised_date(None, None) is None

    def test_a_time_wins_over_the_customers_date(self):
        """A dated order whose slot the plan places on a later day promises that later day."""
        assert promised_date(at(10, 0, day=TODAY + timedelta(days=2)), TOMORROW) == TODAY + timedelta(days=2)


MONDAYS = [date(2026, 10, 12), date(2026, 10, 19), date(2026, 10, 26), date(2026, 11, 2), date(2026, 11, 9)]
NOW = at(11, 0)  # Wednesday 2026-11-04, 11:00 in Tashkent


class TestTheParameterSet:
    def test_with_no_fit_the_seeds_are_the_parameters(self, db):
        params = current_params(NOW)

        assert params.source == "seed"
        assert params.cycle_points == tuple(SEED_POINTS)
        assert params.depot_line == (10.0, 1.5)
        assert params.window_buckets == {}

    def test_current_params_reads_the_latest_fit_before_now(self, db):
        add_fit(db, MONDAYS[1], line=(12.0, 1.4))
        latest = add_fit(db, MONDAYS[3], line=(11.0, 1.6), points=((0.1, 14.0), (0.9, 17.0), (2.4, 25.0)))
        add_fit(db, MONDAYS[4], line=(9.0, 1.9))  # fitted next Monday: it does not exist yet at NOW

        params = current_params(NOW)

        assert params.source == f"fit:{latest.id}"
        assert params.cycle_points == ((0.1, 14.0), (0.9, 17.0), (2.4, 25.0))
        assert params.depot_line == (11.0, 1.6)

    def test_a_fit_exists_from_the_instant_it_is_written(self, db):
        oldest = add_fit(db, MONDAYS[2])
        latest = add_fit(db, MONDAYS[3])
        second = timedelta(seconds=1)

        assert current_params(written_on(MONDAYS[3]) - second).source == f"fit:{oldest.id}"
        assert current_params(written_on(MONDAYS[3])).source == f"fit:{latest.id}"
        assert current_params(written_on(MONDAYS[2]) - second).source == "seed"

    def test_two_fits_written_at_the_same_instant_resolve_to_the_newer_row(self, db):
        stamp = written_on(MONDAYS[3])
        first = add_fit(db, MONDAYS[2], fitted_at=stamp, line=(12.0, 1.4))
        second = add_fit(db, MONDAYS[3], fitted_at=stamp, line=(11.0, 1.6))

        assert second.id > first.id
        assert current_params(NOW).source == f"fit:{second.id}"
        assert current_params(NOW).depot_line == (11.0, 1.6)

    def test_the_parameters_are_immutable_values(self, db):
        add_fit(db, MONDAYS[3])
        params = current_params(NOW)

        assert isinstance(params.cycle_points, tuple) and all(isinstance(point, tuple) for point in params.cycle_points)
        assert isinstance(params.depot_line, tuple)
        with pytest.raises(dataclasses.FrozenInstanceError):
            params.source = "other"

    def test_the_window_buckets_of_a_fit_flow_through_to_lo_and_hi(self, db):
        """What the weekly job stores, read back by the window rules: a fitted bucket, a thin one borrowing the next
        band, and a band with no bucket at all."""
        add_fit(
            db,
            MONDAYS[3],
            buckets={
                "0|started": {"lo": None, "hi": None, "n": 12},
                "1-2|started": {"lo": -33, "hi": 77, "n": 48},
                "1-2|not_started": {"lo": -50, "hi": 140, "n": 41},
            },
        )
        params = current_params(NOW)

        assert lo_hi(params, 1, True) == (-33, 77, False)
        assert lo_hi(params, 0, True) == (-33, 77, False)  # borrowed from "1-2"
        assert lo_hi(params, 2, False) == (-50, 140, False)  # fitted, so no extra hour
        assert lo_hi(params, 5, True) == (-75, 150, True)  # no bucket: the seed
