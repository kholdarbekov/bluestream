from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from business_app.utils.delivery_window import (
    SCHEDULE_BEYOND_HORIZON,
    SCHEDULE_DATE_IN_PAST,
    SCHEDULE_WINDOW_INVALID,
    SCHEDULE_WINDOW_PASSED,
    format_delivery_window,
    local_now,
    parse_and_validate_schedule,
    parse_schedule,
    parse_window_time,
    schedule_date_bounds,
    schedule_error_codes,
    validate_schedule,
    window_kind,
    window_slot_label,
)
from shared.business_config import MAX_SCHEDULE_HORIZON_DAYS

TZ = ZoneInfo("Asia/Tashkent")


@pytest.mark.parametrize(
    "start,end,kind,label",
    [
        (None, None, "anytime", "anytime"),
        (time(12, 0), time(18, 0), "between", "12:00-18:00"),
        (None, time(10, 0), "until", "until 10:00"),
        (time(19, 0), None, "after", "after 19:00"),
    ],
)
def test_the_four_window_shapes(start, end, kind, label):
    assert window_kind(start, end) == kind
    assert format_delivery_window(start, end) == {
        "start": start.strftime("%H:%M") if start else None,
        "end": end.strftime("%H:%M") if end else None,
        "kind": kind,
        "label": label,
    }


def test_slot_label_fits_the_string20_column():
    # Delivery.scheduled_time_slot is String(20) NOT NULL.
    for start, end in [(None, None), (time(12), time(18)), (None, time(10)), (time(19), None)]:
        assert len(window_slot_label(start, end)) <= 20


def test_parse_window_time_accepts_hhmm_and_blank():
    assert parse_window_time("09:00") == time(9, 0)
    assert parse_window_time(None) is None
    assert parse_window_time("") is None
    with pytest.raises(ValueError):
        parse_window_time("9am")


def test_validate_schedule_rejects_past_date():
    now = datetime(2026, 8, 19, 14, 0, tzinfo=TZ)
    errors = validate_schedule(date(2026, 8, 18), None, None, now_local=now)
    assert any("past" in e for e in errors)


def test_validate_schedule_rejects_beyond_horizon():
    now = datetime(2026, 8, 19, 14, 0, tzinfo=TZ)
    errors = validate_schedule(date(2026, 9, 10), None, None, now_local=now)  # 22 days out
    assert any("15" in e for e in errors)


def test_validate_schedule_rejects_inverted_window():
    now = datetime(2026, 8, 19, 14, 0, tzinfo=TZ)
    errors = validate_schedule(date(2026, 8, 20), time(18, 0), time(12, 0), now_local=now)
    assert any("before" in e for e in errors)


def test_validate_schedule_rejects_impossible_same_day_deadline():
    now = datetime(2026, 8, 19, 14, 0, tzinfo=TZ)
    errors = validate_schedule(date(2026, 8, 19), None, time(10, 0), now_local=now)
    assert any("already passed" in e for e in errors)


def test_validate_schedule_accepts_same_day_open_window():
    now = datetime(2026, 8, 19, 14, 0, tzinfo=TZ)
    assert validate_schedule(date(2026, 8, 19), time(19, 0), None, now_local=now) == []


def test_validate_schedule_accepts_no_date():
    now = datetime(2026, 8, 19, 14, 0, tzinfo=TZ)
    assert validate_schedule(None, None, None, now_local=now) == []


def test_validate_schedule_boundary_exactly_15_days_out_is_valid():
    # Horizon check uses `>`, so exactly 15 days is allowed.
    # If the operator is flipped to `>=`, this test fails.
    now = datetime(2026, 8, 19, 14, 0, tzinfo=TZ)
    delivery_date = date(2026, 9, 3)  # exactly 15 days from 2026-08-19
    assert validate_schedule(delivery_date, None, None, now_local=now) == []


def test_validate_schedule_boundary_equal_window_times_rejected():
    # Inverted-window check uses `>=`, so equal start/end is rejected.
    # If the operator is flipped to `>`, this test fails (zero-width window accepted).
    now = datetime(2026, 8, 19, 14, 0, tzinfo=TZ)
    errors = validate_schedule(date(2026, 8, 20), time(12, 0), time(12, 0), now_local=now)
    assert any("before" in e for e in errors)


def test_validate_schedule_boundary_deadline_exactly_now_is_passed():
    # Same-day deadline check uses `<=`, so deadline equal to "now" counts as passed.
    # If the operator is flipped to `<`, this test fails (deadline exactly now is accepted).
    now = datetime(2026, 8, 19, 14, 0, tzinfo=TZ)
    errors = validate_schedule(date(2026, 8, 19), None, time(14, 0), now_local=now)
    assert any("already passed" in e for e in errors)


# --- parse_and_validate_schedule: the one entry point both write paths use ----


def test_blank_strings_mean_no_schedule_at_all():
    """What an HTML form posts for "no date chosen" is "", not null. If "" were
    treated as a malformed date the web checkout would 400 on every order that
    simply did not pick a day."""
    now = datetime(2026, 8, 19, 14, 0, tzinfo=TZ)
    assert parse_and_validate_schedule("", "", "", now_local=now) == (None, None, None, [])


def test_none_is_also_no_schedule():
    now = datetime(2026, 8, 19, 14, 0, tzinfo=TZ)
    assert parse_and_validate_schedule(None, None, None, now_local=now) == (None, None, None, [])


def test_parses_a_real_schedule_into_typed_values():
    now = datetime(2026, 8, 19, 14, 0, tzinfo=TZ)
    assert parse_and_validate_schedule("2026-08-20", "12:00", "18:00", now_local=now) == (
        date(2026, 8, 20),
        time(12, 0),
        time(18, 0),
        [],
    )


def test_an_already_parsed_date_is_accepted_as_well_as_an_iso_string():
    """The admin path hands over a raw JSON string; the public path hands over a
    Pydantic-parsed `date`. Both must land on the same value rather than the
    public path having to re-serialise a date just to re-parse it."""
    now = datetime(2026, 8, 19, 14, 0, tzinfo=TZ)
    from_string = parse_and_validate_schedule("2026-08-20", None, None, now_local=now)
    from_date = parse_and_validate_schedule(date(2026, 8, 20), None, None, now_local=now)
    assert from_string == from_date == (date(2026, 8, 20), None, None, [])


MALFORMED_SCHEDULES = [
    ("20-08-2026", None, None),   # wrong order
    ("2026-13-01", None, None),   # not a real month
    ("2026-08-20", "25:99", None),  # not a real time
    ("2026-08-20", None, "noon"),   # not a time at all
    (12345, None, None),            # not even a string
    ("2026-08-20", 900, None),      # a number where "HH:MM" belongs: AttributeError, not ValueError
]


@pytest.mark.parametrize("raw_date,raw_start,raw_end", MALFORMED_SCHEDULES)
def test_malformed_input_is_an_error_string_never_an_exception(raw_date, raw_start, raw_end):
    """A typo in an operator's form must surface as a 400, not a 500: this
    helper is what stands between `fromisoformat` and the endpoint's blanket
    `except Exception`."""
    now = datetime(2026, 8, 19, 14, 0, tzinfo=TZ)
    parsed_date, start, end, errors = parse_and_validate_schedule(raw_date, raw_start, raw_end, now_local=now)
    assert (parsed_date, start, end) == (None, None, None)
    assert len(errors) == 1 and errors[0].startswith("Invalid delivery schedule")


def test_a_valid_shape_still_gets_the_schedule_rule_applied():
    """Parsing succeeding is not the same as the schedule being allowed — the
    helper must pass the parsed values on to `validate_schedule`, not return
    them unchecked."""
    now = datetime(2026, 8, 19, 14, 0, tzinfo=TZ)
    _, _, _, errors = parse_and_validate_schedule("2026-08-18", None, None, now_local=now)
    assert errors == ["delivery_date cannot be in the past"]


def test_the_clock_defaults_to_business_local_time_when_not_injected():
    """`now_local` is optional so no caller has to decide which clock `today`
    means. Omitting it must behave exactly as passing the business-local now."""
    assert parse_and_validate_schedule("", None, None) == (None, None, None, [])

    today_local = local_now().date()
    _, _, _, errors = parse_and_validate_schedule(
        (today_local - timedelta(days=1)).isoformat(), None, None
    )
    assert errors == ["delivery_date cannot be in the past"]

    _, _, _, ok = parse_and_validate_schedule(
        (today_local + timedelta(days=1)).isoformat(), None, None
    )
    assert ok == []


# --- parse_schedule / schedule_error_codes: the two halves (F10) ----------------
#
# The reschedule PATCH parses with `parse_schedule` alone, and `reschedule` maps
# `schedule_error_codes` to its own coded refusals
# (docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md §3.5).

AFTERNOON = datetime(2026, 8, 19, 14, 0, tzinfo=TZ)


def test_parse_schedule_types_the_three_values_and_blank_means_not_set():
    assert parse_schedule("2026-08-20", "12:00", "18:00") == (date(2026, 8, 20), time(12, 0), time(18, 0))
    assert parse_schedule(date(2026, 8, 20), None, "10:00") == (date(2026, 8, 20), None, time(10, 0))
    assert parse_schedule("", "", "") == (None, None, None)
    assert parse_schedule(None, None, None) == (None, None, None)


def test_parse_schedule_judges_nothing():
    """Parsing only. A past date and an inverted window both parse: the PATCH hands them to
    `reschedule`, which refuses them with codes the modal can put into words."""
    assert parse_schedule("2020-01-01", "18:00", "12:00") == (date(2020, 1, 1), time(18, 0), time(12, 0))


@pytest.mark.parametrize("raw_date,raw_start,raw_end", MALFORMED_SCHEDULES)
def test_parse_schedule_raises_one_value_error_for_anything_malformed(raw_date, raw_start, raw_end):
    """One exception type, so the PATCH's `except ValueError` answers every typo with a 400. An
    `AttributeError` or `TypeError` escaping from here would reach the endpoint's 500."""
    with pytest.raises(ValueError) as excinfo:
        parse_schedule(raw_date, raw_start, raw_end)
    assert str(excinfo.value).startswith("Invalid delivery schedule: ")


def test_the_codes_are_the_contracted_strings():
    assert (SCHEDULE_DATE_IN_PAST, SCHEDULE_BEYOND_HORIZON, SCHEDULE_WINDOW_INVALID, SCHEDULE_WINDOW_PASSED) == (
        "DATE_IN_PAST",
        "BEYOND_HORIZON",
        "WINDOW_INVALID",
        "WINDOW_PASSED",
    )


@pytest.mark.parametrize(
    "days,start,end,codes",
    [
        (1, None, None, []),
        (0, time(19, 0), None, []),  # "after 19:00" today at 14:00 is late at worst, not impossible
        (MAX_SCHEDULE_HORIZON_DAYS, None, None, []),  # the last bookable day
        (-1, None, None, [SCHEDULE_DATE_IN_PAST]),
        (MAX_SCHEDULE_HORIZON_DAYS + 1, None, None, [SCHEDULE_BEYOND_HORIZON]),
        (1, time(18, 0), time(12, 0), [SCHEDULE_WINDOW_INVALID]),
        (1, time(12, 0), time(12, 0), [SCHEDULE_WINDOW_INVALID]),
        (0, None, time(14, 0), [SCHEDULE_WINDOW_PASSED]),  # a deadline of exactly now has passed
        (0, time(9, 0), time(12, 0), [SCHEDULE_WINDOW_PASSED]),
        (0, time(13, 0), time(12, 0), [SCHEDULE_WINDOW_INVALID, SCHEDULE_WINDOW_PASSED]),
        (-1, time(18, 0), time(12, 0), [SCHEDULE_DATE_IN_PAST, SCHEDULE_WINDOW_INVALID]),
    ],
)
def test_schedule_error_codes_names_every_broken_rule_in_rule_order(days, start, end, codes):
    delivery_date = AFTERNOON.date() + timedelta(days=days)
    assert schedule_error_codes(delivery_date, start, end, now_local=AFTERNOON) == codes


def test_schedule_error_codes_without_a_date_judges_only_the_window_shape():
    assert schedule_error_codes(None, None, None, now_local=AFTERNOON) == []
    # No day, so no window can have ended yet.
    assert schedule_error_codes(None, time(9, 0), time(12, 0), now_local=AFTERNOON) == []
    assert schedule_error_codes(None, time(18, 0), time(12, 0), now_local=AFTERNOON) == [SCHEDULE_WINDOW_INVALID]


def test_validate_schedule_words_each_code_exactly_as_before():
    """Create-order, checkout, `order_validators`, the outlet PUT and the sales visit order match
    these sentences, and tests across the suite pin them. Mapping codes to them must not reword one."""
    today = AFTERNOON.date()
    assert validate_schedule(today - timedelta(days=1), time(13, 0), time(12, 0), now_local=AFTERNOON) == [
        "delivery_date cannot be in the past",
        "window_start must be before window_end",
    ]
    too_far = today + timedelta(days=MAX_SCHEDULE_HORIZON_DAYS + 1)
    assert validate_schedule(too_far, None, None, now_local=AFTERNOON) == [
        f"delivery_date cannot be more than {MAX_SCHEDULE_HORIZON_DAYS} days in the future"
    ]
    assert validate_schedule(today, time(13, 0), time(12, 0), now_local=AFTERNOON) == [
        "window_start must be before window_end",
        "delivery window has already passed for today",
    ]


# --- schedule_date_bounds: the one statement of the horizon ---------------------


def test_schedule_date_bounds_is_local_today_through_the_horizon():
    now = datetime(2026, 8, 19, 14, 0, tzinfo=TZ)
    assert schedule_date_bounds(now) == (
        date(2026, 8, 19),
        date(2026, 8, 19) + timedelta(days=MAX_SCHEDULE_HORIZON_DAYS),
    )


def test_validate_schedule_accepts_exactly_the_bounds_it_publishes():
    """The published range and the checked range are one range. The admin picker
    offers `schedule_date_bounds()`; if `validate_schedule` kept its own copy, the
    first and last day could drift apart from what the picker shows."""
    now = datetime(2026, 8, 19, 14, 0, tzinfo=TZ)
    first, last = schedule_date_bounds(now)

    assert validate_schedule(first, None, None, now_local=now) == []
    assert validate_schedule(last, None, None, now_local=now) == []
    assert validate_schedule(first - timedelta(days=1), None, None, now_local=now) == [
        "delivery_date cannot be in the past"
    ]
    assert validate_schedule(last + timedelta(days=1), None, None, now_local=now) == [
        f"delivery_date cannot be more than {MAX_SCHEDULE_HORIZON_DAYS} days in the future"
    ]


def test_schedule_date_bounds_defaults_to_the_business_clock(monkeypatch):
    """00:30 in Tashkent is 19:30 the previous day in UTC. A caller that used the
    container's UTC date would publish yesterday as the first bookable day."""
    frozen = datetime(2026, 9, 24, 0, 30, tzinfo=TZ)
    monkeypatch.setattr("business_app.utils.delivery_window.local_now", lambda: frozen)

    assert schedule_date_bounds()[0] == date(2026, 9, 24)
