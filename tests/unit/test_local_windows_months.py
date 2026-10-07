"""Pay months in `business_app/utils/local_windows.py` (spec 2026-09-28 §4.2).

A pay month is a first-of-month DATE bounded by LOCAL midnights. Tashkent is UTC+5 with no
daylight saving, so every month starts at 19:00 UTC on the day before; each boundary below is
written in UTC so the five-hour offset shows in the literal. Nothing here reads a clock.
"""

from datetime import date, datetime, timezone

import pytest

from business_app.utils.exceptions import ValidationError
from business_app.utils.local_windows import (
    format_month,
    local_month,
    month_bounds,
    month_end,
    month_start,
    next_month,
    parse_month,
    previous_month,
)

pytestmark = pytest.mark.unit

UTC = timezone.utc
SEPTEMBER, OCTOBER, NOVEMBER = date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1)


def test_a_month_is_named_by_its_first_day():
    assert month_start(date(2026, 10, 17)) == OCTOBER
    assert month_start(date(2026, 10, 31)) == OCTOBER
    assert month_start(OCTOBER) == OCTOBER


@pytest.mark.parametrize(
    "month,last_day",
    [
        (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2028, 2, 1), date(2028, 2, 29)),  # a leap year
        (date(2026, 4, 1), date(2026, 4, 30)),
        (date(2026, 10, 1), date(2026, 10, 31)),
        (date(2026, 12, 1), date(2026, 12, 31)),
    ],
)
def test_month_end_is_the_months_last_calendar_day(month, last_day):
    assert month_end(month) == last_day


def test_next_and_previous_month_cross_the_year():
    assert next_month(OCTOBER) == NOVEMBER
    assert next_month(date(2026, 12, 1)) == date(2027, 1, 1)
    assert previous_month(NOVEMBER) == OCTOBER
    assert previous_month(date(2027, 1, 1)) == date(2026, 12, 1)
    assert previous_month(date(2026, 3, 1)) == date(2026, 2, 1)


def test_the_local_month_turns_at_tashkent_midnight_not_utc():
    # 2026-10-01 00:00 Tashkent is 2026-09-30 19:00 UTC.
    assert local_month(datetime(2026, 9, 30, 18, 59, 59, tzinfo=UTC)) == SEPTEMBER
    assert local_month(datetime(2026, 9, 30, 19, 0, tzinfo=UTC)) == OCTOBER
    # Delivered and paid at 23:50 local on the month's last day: the older month (Review Focus 4).
    assert local_month(datetime(2026, 10, 31, 18, 50, tzinfo=UTC)) == OCTOBER
    # A naive instant is UTC, the shape a timestamp comes back in from SQLite.
    assert local_month(datetime(2026, 9, 30, 19, 0)) == OCTOBER


def test_a_months_bounds_are_its_local_midnights_and_consecutive_months_tile():
    assert month_bounds(OCTOBER) == (
        datetime(2026, 9, 30, 19, 0, tzinfo=UTC),
        datetime(2026, 10, 31, 19, 0, tzinfo=UTC),
    )
    # Half-open, so a payment at exactly local midnight belongs to the new month, once.
    assert month_bounds(OCTOBER)[1] == month_bounds(NOVEMBER)[0]
    assert month_bounds(date(2026, 12, 1))[1] == month_bounds(date(2027, 1, 1))[0]


def test_a_month_reads_from_and_writes_to_yyyy_mm():
    assert parse_month("2026-10") == OCTOBER
    assert parse_month("2027-01") == date(2027, 1, 1)
    assert format_month(OCTOBER) == "2026-10"
    assert format_month(date(2027, 1, 1)) == "2027-01"
    assert parse_month(format_month(date(2026, 12, 1))) == date(2026, 12, 1)


@pytest.mark.parametrize(
    "value",
    [
        "2026-13",
        "2026-00",
        "2026-1",
        "26-10",
        "2026-10-01",
        "2026/10",
        "october",
        "",
        " 2026-10",
        "0000-01",
        None,
        202610,
    ],
)
def test_anything_else_is_one_coded_refusal(value):
    """The admin pay routes and the agent's lines query both parse here: one typo, one answer.
    Strict on purpose, because a month guessed from "2026-1" could close or pay the wrong one."""
    with pytest.raises(ValidationError) as refused:
        parse_month(value)

    assert refused.value.error_code == "SALES_PAY_MONTH_INVALID"
    assert refused.value.message == "Month must be YYYY-MM"
    assert refused.value.details == {"month": value}
