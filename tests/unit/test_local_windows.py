"""`business_app/utils/local_windows.py` — one local calendar, six windows.

Every instant here is FROZEN and handed in (`now=`), the contract the digest and
the nightly jobs already keep (`tests/unit/test_agent_digest_service.py:3-7`): a
window is arithmetic about ONE moment, and a helper that re-read the wall clock
halfway through would answer differently at 04:59 and 05:01 Tashkent with no test
able to say so.

`FROZEN_UTC` is 2026-09-15 20:20 UTC = 2026-09-16 01:20 Tashkent — the wall time
`snapshot-sales-agent-day-plans` fires at. The UTC date and the local date
DISAGREE there, so every assertion below fails against a UTC-midnight
implementation instead of passing by coincidence.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from business_app.services.sales.replenishment_service import ReplenishmentService
from business_app.utils import local_windows
from business_app.utils.exceptions import ValidationError
from business_app.utils.local_windows import (
    STATS_PERIODS,
    days_inclusive,
    local_date,
    local_day_bounds,
    parse_date_range,
    period_window,
    previous_local_week,
    resolve_stats_period,
    window_bounds,
)
from shared.constants import DISPLAY_TIMEZONE

pytestmark = pytest.mark.unit

# 01:20 Tashkent on 2026-09-16 — the snapshot job's own moment, one local day
# AHEAD of its UTC date.
FROZEN_UTC = datetime(2026, 9, 15, 20, 20, tzinfo=timezone.utc)
# 05:30 Tashkent on the SAME local day: this time the UTC date has already rolled
# over and the local one has not moved. The two failures are opposite in sign.
FROZEN_MORNING_UTC = datetime(2026, 9, 16, 0, 30, tzinfo=timezone.utc)
LOCAL_TODAY = date(2026, 9, 16)  # a Wednesday
LOCAL_MIDNIGHT_UTC = datetime(2026, 9, 15, 19, 0, tzinfo=timezone.utc)
NEXT_LOCAL_MIDNIGHT_UTC = datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc)
# The same instant `local_now()` itself would answer with: a LOCAL-zone datetime, so a
# frozen `local_now` behaves like the real one (`local_date(None)` reads `.date()` off it,
# and `_tz()` reads its tzinfo).
FROZEN_LOCAL = FROZEN_UTC.astimezone(ZoneInfo(DISPLAY_TIMEZONE))


def test_the_local_day_is_not_the_utc_day_in_either_direction():
    assert local_date(FROZEN_UTC) == LOCAL_TODAY  # UTC still says the 15th
    assert local_date(FROZEN_MORNING_UTC) == LOCAL_TODAY  # UTC already says the 16th
    # A naive instant is UTC — the shape every DateTime column round-trips out of
    # SQLite as, and the reason `ensure_utc` exists.
    assert local_date(FROZEN_UTC.replace(tzinfo=None)) == LOCAL_TODAY


def test_a_local_day_is_a_half_open_utc_pair():
    assert local_day_bounds(LOCAL_TODAY) == (LOCAL_MIDNIGHT_UTC, NEXT_LOCAL_MIDNIGHT_UTC)
    # Half-open, so consecutive days tile without sharing a single second: a visit
    # at exactly local midnight belongs to the new day, once.
    assert local_day_bounds(LOCAL_TODAY)[1] == local_day_bounds(LOCAL_TODAY + timedelta(days=1))[0]


def test_a_window_runs_from_the_first_days_midnight_to_the_last_days_end():
    assert window_bounds(date(2026, 9, 14), LOCAL_TODAY) == (
        datetime(2026, 9, 13, 19, 0, tzinfo=timezone.utc),
        NEXT_LOCAL_MIDNIGHT_UTC,
    )
    assert days_inclusive(date(2026, 9, 14), LOCAL_TODAY) == 3
    assert days_inclusive(LOCAL_TODAY, LOCAL_TODAY) == 1


def test_an_inverted_range_has_no_number_of_days():
    """`visits_per_day` divides by this. A negative or zero divisor must be a
    raise here, not a silently negative rate three layers up."""
    with pytest.raises(ValueError):
        days_inclusive(LOCAL_TODAY, date(2026, 9, 14))


def test_each_stats_period_is_one_inclusive_local_range():
    assert STATS_PERIODS == ("today", "week", "month")
    assert period_window("today", now=FROZEN_UTC) == (LOCAL_TODAY, LOCAL_TODAY)
    # 2026-09-16 is a Wednesday: the week runs Monday the 14th to TODAY, never to
    # Sunday the 20th — a week-to-date card must not divide by days that have not
    # happened yet.
    assert period_window("week", now=FROZEN_UTC) == (date(2026, 9, 14), LOCAL_TODAY)
    assert period_window("month", now=FROZEN_UTC) == (date(2026, 9, 1), LOCAL_TODAY)


def test_an_unknown_period_is_refused_rather_than_defaulted():
    """A typo'd period must not silently render "today" under a "month" heading."""
    with pytest.raises(ValueError):
        period_window("quarter", now=FROZEN_UTC)


class TestResolveStatsPeriod:
    """`?period=` on `GET /staff/sales/me/stats`, resolved once (R31).

    Unlike every other helper here, `resolve_stats_period` takes no `now`: it is the wire's
    entry point and reads the WALL clock through `period_window`. So the clock is frozen on
    `local_windows.local_now` -- the MODULE ATTRIBUTE, the same seam the HTTP window tests
    freeze. Patching `delivery_window.local_now` instead would reach nothing (the name is
    already bound here) and these assertions would start depending on the calendar.
    """

    @pytest.fixture(autouse=True)
    def frozen_local_now(self, monkeypatch):
        monkeypatch.setattr(local_windows, "local_now", lambda: FROZEN_LOCAL)

    def test_each_period_resolves_to_its_own_window(self):
        """The name travels back with the dates, and the dates are `period_window`'s --
        the resolver translates an error, it does not own a second calendar."""
        for period in STATS_PERIODS:
            assert resolve_stats_period(period) == (period, *period_window(period, now=FROZEN_UTC))
        assert resolve_stats_period("today") == ("today", LOCAL_TODAY, LOCAL_TODAY)
        assert resolve_stats_period("week") == ("week", date(2026, 9, 14), LOCAL_TODAY)
        assert resolve_stats_period("month") == ("month", date(2026, 9, 1), LOCAL_TODAY)

    def test_an_absent_or_empty_period_is_today(self):
        """The bot's first render sends no `period=` and a cleared query string arrives as
        the empty string. Both mean the default, decided HERE rather than once per caller."""
        assert resolve_stats_period(None) == ("today", LOCAL_TODAY, LOCAL_TODAY)
        assert resolve_stats_period("") == ("today", LOCAL_TODAY, LOCAL_TODAY)

    def test_an_unknown_period_is_a_coded_refusal_not_a_bare_value_error(self):
        """`period_window` raises `ValueError` -- the right shape for a library call and the
        wrong one for the wire, where an uncoded 400 reaches the staff bot as the generic
        "Something went wrong"."""
        with pytest.raises(ValidationError) as excinfo:
            resolve_stats_period("quarter")
        assert excinfo.value.error_code == "SALES_STATS_PERIOD_INVALID"
        assert excinfo.value.details == {"period": "quarter", "allowed": list(STATS_PERIODS)}


def test_the_previous_local_week_is_monday_to_sunday_and_never_touches_this_one():
    monday, sunday = previous_local_week(FROZEN_UTC)

    assert (monday, sunday) == (date(2026, 9, 7), date(2026, 9, 13))
    assert monday.weekday() == 0 and sunday.weekday() == 6
    # The Monday-morning report and the week-to-date card cannot overlap by a day.
    assert sunday < period_window("week", now=FROZEN_UTC)[0]


class TestParseDateRange:
    def test_absent_dates_are_the_last_seven_local_days_including_today(self, app):
        with app.app_context():
            assert parse_date_range(None, None, now=FROZEN_UTC) == (date(2026, 9, 10), LOCAL_TODAY)
            # An empty query-string value is "not asked", not "the epoch".
            assert parse_date_range("", "  ", now=FROZEN_UTC) == (date(2026, 9, 10), LOCAL_TODAY)

    def test_a_half_given_range_fills_the_other_end(self, app):
        """An operator who types only a start means "since then", not "no data"."""
        with app.app_context():
            assert parse_date_range("2026-09-01", None, now=FROZEN_UTC) == (date(2026, 9, 1), LOCAL_TODAY)
            assert parse_date_range(None, "2026-09-16", now=FROZEN_UTC) == (date(2026, 9, 10), LOCAL_TODAY)

    def test_both_given_are_taken_verbatim(self, app):
        with app.app_context():
            assert parse_date_range("2026-09-01", "2026-09-03", now=FROZEN_UTC) == (
                date(2026, 9, 1),
                date(2026, 9, 3),
            )

    @pytest.mark.parametrize(
        "start,end",
        [
            ("yesterday", None),
            ("2026-13-01", None),
            (None, "16/09/2026"),
            ("2026-09-03", "2026-09-01"),
        ],
    )
    def test_a_range_that_cannot_be_honoured_is_one_error_code(self, app, start, end):
        """One parser, one code: five read routes ask this question and a per-route
        `date.fromisoformat` would give five different answers to one typo."""
        with app.app_context():
            with pytest.raises(ValidationError) as excinfo:
                parse_date_range(start, end, now=FROZEN_UTC)
            assert excinfo.value.error_code == "SALES_DATE_RANGE_INVALID"

    def test_the_cap_is_the_configured_number_of_days_not_a_literal(self, app):
        """92 days passes, 93 does not — and the boundary moves with the config."""
        with app.app_context():
            assert app.config["SALES_METRICS_MAX_RANGE_DAYS"] == 92
            assert parse_date_range("2026-06-17", "2026-09-16", now=FROZEN_UTC) == (
                date(2026, 6, 17),
                date(2026, 9, 16),
            )
            with pytest.raises(ValidationError) as excinfo:
                parse_date_range("2026-06-16", "2026-09-16", now=FROZEN_UTC)
            assert excinfo.value.error_code == "SALES_DATE_RANGE_INVALID"


def test_the_due_lists_day_boundary_is_this_modules_boundary(app, monkeypatch):
    """One definition of the local day: the due list and the driver's day cannot drift.

    `ReplenishmentService.local_day_end_utc` now derives from `local_day_bounds`
    instead of `_driver_day_start_utc` + 1 day, so BOTH clocks are frozen at the
    same instant and the equality is asserted ACROSS the two modules — which is a
    stronger statement than the single-module version it replaces, and the only
    one that can catch the two definitions parting company.
    """
    from business_app.services import route_optimization_service as ros
    from business_app.utils import delivery_window

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return FROZEN_UTC.astimezone(tz) if tz else FROZEN_UTC.replace(tzinfo=None)

    monkeypatch.setattr(ros, "datetime", _FrozenDatetime)
    monkeypatch.setattr(delivery_window, "datetime", _FrozenDatetime)

    with app.app_context():
        assert ReplenishmentService.local_day_end_utc(FROZEN_UTC) == NEXT_LOCAL_MIDNIGHT_UTC
        assert ReplenishmentService.local_day_end_utc() == NEXT_LOCAL_MIDNIGHT_UTC
        assert ReplenishmentService.local_day_end_utc() == ros._driver_day_start_utc() + timedelta(days=1)
