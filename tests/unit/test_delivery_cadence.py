"""`delivery_cadence`: the ONE usual-gap rule (spec 2026-10-09 §4 "Usual gap", §9.3, D6).

Two readers ask it and neither restates it: the outlet stage job (`min_days=2`) and the reorder
reminders (`min_days=REORDER_REMINDER_MIN_DELIVERIES`). Every expectation below is the spec's
own arithmetic on calendar dates chosen so the answer is a literal, never re-derived here.

Instants are built as Tashkent wall-clock times (`DISPLAY_TIMEZONE`), because a delivery DAY is a
local date: 01:00 on the 20th in Tashkent is 20:00 on the 19th in UTC.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from business_app.services.sales import outlet_service
from business_app.utils.delivery_cadence import (
    CADENCE_MIN_GAP_DAYS,
    CADENCE_SAMPLE_GAPS,
    delivery_days,
    typical_interval_days,
)
from shared.constants import DISPLAY_TIMEZONE

pytestmark = pytest.mark.unit

TASHKENT = ZoneInfo(DISPLAY_TIMEZONE)


def _local(day: date, hour: int = 11, minute: int = 0) -> datetime:
    """A delivery at `hour:minute` Tashkent time on `day`."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TASHKENT)


def _days_before(start: date, *offsets: int):
    return [_local(start - timedelta(days=offset)) for offset in offsets]


def test_the_constants_are_the_specs():
    assert (CADENCE_SAMPLE_GAPS, CADENCE_MIN_GAP_DAYS) == (5, 7)


def test_the_outlet_job_no_longer_carries_its_own_copy_of_the_constants():
    """§9.3: INTERVAL_SAMPLE_SIZE / MIN_INTERVAL_DAYS MOVED here; a second copy is a second rule."""
    assert not hasattr(outlet_service, "INTERVAL_SAMPLE_SIZE")
    assert not hasattr(outlet_service, "MIN_INTERVAL_DAYS")


def test_delivery_days_are_distinct_local_dates_newest_first():
    instants = [
        _local(date(2026, 9, 12), 23, 0),
        _local(date(2026, 9, 20), 1, 0),
        _local(date(2026, 9, 20), 15, 0),
        _local(date(2026, 9, 12), 9, 0),
    ]

    assert delivery_days(instants) == [date(2026, 9, 20), date(2026, 9, 12)]


def test_a_delivery_day_is_the_tashkent_date_not_the_utc_date():
    """20:00 UTC on the 19th is 01:00 on the 20th in Tashkent. A naive instant is UTC (SQLite)."""
    assert delivery_days([datetime(2026, 9, 19, 20, 0, tzinfo=UTC)]) == [date(2026, 9, 20)]
    assert delivery_days([datetime(2026, 9, 19, 20, 0)]) == [date(2026, 9, 20)]


def test_an_instant_that_does_not_exist_is_no_delivery_day():
    """Review Focus 2: an order with no delivered instant must not be dated "today"."""
    assert delivery_days([None, _local(date(2026, 9, 12))]) == [date(2026, 9, 12)]


def test_too_few_delivery_days_is_no_rhythm():
    one_day_twice = [_local(date(2026, 9, 20), 9), _local(date(2026, 9, 20), 15)]
    two_days = _days_before(date(2026, 9, 20), 0, 14)

    assert typical_interval_days([], min_days=2) is None
    assert typical_interval_days(one_day_twice, min_days=2) is None
    assert typical_interval_days(two_days, min_days=3) is None
    # One day has no gap, whatever the caller asks for.
    assert typical_interval_days(_days_before(date(2026, 9, 20), 0), min_days=1) is None


def test_gaps_are_whole_local_days():
    """23:00 on 12 Sep and 01:00 on 20 Sep are 7 days 2 hours apart but 8 calendar days."""
    instants = [_local(date(2026, 9, 12), 23, 0), _local(date(2026, 9, 20), 1, 0)]

    assert typical_interval_days(instants, min_days=2) == 8


def test_a_same_day_burst_is_one_delivery_day():
    """D6: four orders on three days are three delivery days (gaps 14, 14)."""
    start = date(2026, 10, 1)
    instants = [_local(start, 8), _local(start, 17)] + _days_before(start, 14, 28)

    assert typical_interval_days(instants, min_days=3) == 14
    assert typical_interval_days(instants, min_days=4) is None


@pytest.mark.parametrize(
    "days, expected",
    [
        ((date(2026, 9, 22), date(2026, 9, 11), date(2026, 9, 1)), 11),  # gaps 11, 10 -> 10.5 -> 11
        ((date(2026, 9, 18), date(2026, 9, 10), date(2026, 9, 1)), 9),  # gaps 8, 9 -> 8.5 -> 9
    ],
    ids=["10.5-rounds-to-11", "8.5-rounds-to-9"],
)
def test_the_median_rounds_half_up(days, expected):
    assert typical_interval_days([_local(day) for day in days], min_days=2) == expected


def test_the_median_is_floored_at_seven_days():
    daily = _days_before(date(2026, 10, 1), 0, 1, 2, 3, 4, 5)

    assert typical_interval_days(daily, min_days=2) == CADENCE_MIN_GAP_DAYS


def test_only_the_newest_six_delivery_days_count():
    """Gaps newest first: 20, 20, 20, 10, 10 | 10, 10, 10. The window's median is 20; all eight
    gaps would give 10."""
    instants = _days_before(date(2026, 10, 1), 0, 20, 40, 60, 70, 80, 90, 100, 110)

    assert typical_interval_days(instants, min_days=2) == 20


def test_the_input_order_does_not_matter():
    instants = _days_before(date(2026, 10, 1), 40, 0, 20)

    assert typical_interval_days(instants, min_days=2) == 20
