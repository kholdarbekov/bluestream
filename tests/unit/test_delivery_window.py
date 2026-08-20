from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from business_app.utils.delivery_window import (
    format_delivery_window,
    local_now,
    parse_and_validate_schedule,
    parse_window_time,
    validate_schedule,
    window_kind,
    window_slot_label,
)

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


@pytest.mark.parametrize(
    "raw_date,raw_start,raw_end",
    [
        ("20-08-2026", None, None),   # wrong order
        ("2026-13-01", None, None),   # not a real month
        ("2026-08-20", "25:99", None),  # not a real time
        ("2026-08-20", None, "noon"),   # not a time at all
        (12345, None, None),            # not even a string
    ],
)
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
