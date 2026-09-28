"""The delivery time window — one `(start, end)` pair, either side nullable.

SSOT for what a window MEANS. Four shapes, and only four:

    (None, None)          anytime that day
    (12:00, 18:00)        between
    (None, 10:00)         until  — a deadline
    (19:00, None)         after  — an earliest time

Pure: no DB, no Flask app context, no imports from `business_app` beyond
`shared`. Alembic and both bots can import it.

The window is ADVISORY (spec D5): it orders the driver pool and is displayed
everywhere, but nothing blocks a delivery outside it.
"""

from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from shared.business_config import MAX_SCHEDULE_HORIZON_DAYS
from shared.constants import DISPLAY_TIMEZONE

ANYTIME = "anytime"
BETWEEN = "between"
UNTIL = "until"
AFTER = "after"

# What `schedule_error_codes` can find wrong with a requested schedule.
# `OrderScheduleService.reschedule` maps each one to its own coded refusal (F10);
# `validate_schedule` words them for the paths that answer with sentences.
SCHEDULE_DATE_IN_PAST = "DATE_IN_PAST"
SCHEDULE_BEYOND_HORIZON = "BEYOND_HORIZON"
SCHEDULE_WINDOW_INVALID = "WINDOW_INVALID"
SCHEDULE_WINDOW_PASSED = "WINDOW_PASSED"


def window_kind(start: Optional[time], end: Optional[time]) -> str:
    """Classify a window. The one place the four shapes are named."""
    if start is None and end is None:
        return ANYTIME
    if start is None:
        return UNTIL
    if end is None:
        return AFTER
    return BETWEEN


def _hhmm(value: Optional[time]) -> Optional[str]:
    return value.strftime("%H:%M") if value is not None else None


def format_delivery_window(start: Optional[time], end: Optional[time]) -> Dict[str, Any]:
    """The payload every order response publishes as `delivery_window`.

    `kind` is the machine-readable answer clients translate from; `label` is an
    English fallback for logs and non-localized surfaces. Clients MUST branch on
    `kind` and render their own localized string — they must never re-derive the
    shape from `start`/`end` themselves.
    """
    kind = window_kind(start, end)
    if kind == ANYTIME:
        label = "anytime"
    elif kind == UNTIL:
        label = f"until {_hhmm(end)}"
    elif kind == AFTER:
        label = f"after {_hhmm(start)}"
    else:
        label = f"{_hhmm(start)}-{_hhmm(end)}"
    return {"start": _hhmm(start), "end": _hhmm(end), "kind": kind, "label": label}


def window_slot_label(start: Optional[time], end: Optional[time]) -> str:
    """Value for `Delivery.scheduled_time_slot`, which is String(20) NOT NULL.

    Every existing delivery row carries the hardcoded "09:00-12:00" that
    `create_delivery` used to write regardless of what the customer asked for.
    """
    return format_delivery_window(start, end)["label"][:20]


def parse_window_time(raw: Optional[str]) -> Optional[time]:
    """Parse "HH:MM" from a request payload. Blank and None both mean "open"."""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    return time.fromisoformat(raw)  # raises ValueError on anything else


def schedule_date_bounds(now_local: Optional[datetime] = None) -> Tuple[date, date]:
    """The first and last day a delivery may be scheduled for, both inclusive.

    `(local today, local today + MAX_SCHEDULE_HORIZON_DAYS)`. The ONE statement
    of the horizon: `schedule_error_codes` checks against it, and
    `GET /orders/statuses` publishes it for the admin date picker. That way the
    picker can never offer a day the write path refuses, or hide one it
    accepts. `now_local` defaults to `local_now()`, the business clock, which
    is looked up at call time so tests can freeze it.
    """
    if now_local is None:
        now_local = local_now()
    today = now_local.date()
    return today, today + timedelta(days=MAX_SCHEDULE_HORIZON_DAYS)


def schedule_error_codes(
    delivery_date: Optional[date],
    window_start: Optional[time],
    window_end: Optional[time],
    *,
    now_local: datetime,
) -> List[str]:
    """What is wrong with a requested schedule, as `SCHEDULE_*` codes. Empty list == valid.

    The ONE statement of the schedule rule. `validate_schedule` words it for the
    paths that answer in sentences, and `OrderScheduleService.reschedule` maps it
    to coded refusals, so the rule cannot drift between them. Codes come in rule
    order: the date, then the window's shape, then whether it has ended today.
    `now_local` is injected rather than read so the caller owns the clock and
    tests can freeze it.
    """
    codes: List[str] = []
    today, last_day = schedule_date_bounds(now_local)

    if delivery_date is not None:
        if delivery_date < today:
            codes.append(SCHEDULE_DATE_IN_PAST)
        elif delivery_date > last_day:
            codes.append(SCHEDULE_BEYOND_HORIZON)

    if window_start is not None and window_end is not None and window_start >= window_end:
        codes.append(SCHEDULE_WINDOW_INVALID)

    # An "until 10:00" order placed at 14:00 today is a promise that cannot be
    # kept. Only the END is checked: "after 19:00" today at 20:00 is merely late,
    # not impossible.
    if delivery_date is not None and delivery_date == today and window_end is not None:
        if window_end <= now_local.time():
            codes.append(SCHEDULE_WINDOW_PASSED)

    return codes


# `validate_schedule`'s sentence for each code. Callers and tests across the
# suite match these exact strings, so they are kept word for word.
_SCHEDULE_ERROR_MESSAGES = {
    SCHEDULE_DATE_IN_PAST: "delivery_date cannot be in the past",
    SCHEDULE_BEYOND_HORIZON: f"delivery_date cannot be more than {MAX_SCHEDULE_HORIZON_DAYS} days in the future",
    SCHEDULE_WINDOW_INVALID: "window_start must be before window_end",
    SCHEDULE_WINDOW_PASSED: "delivery window has already passed for today",
}


def validate_schedule(
    delivery_date: Optional[date],
    window_start: Optional[time],
    window_end: Optional[time],
    *,
    now_local: datetime,
) -> List[str]:
    """Validate a requested schedule, in English. Empty list == valid.

    `schedule_error_codes` worded, one sentence per code, for the paths that
    answer with sentences: create-order, web checkout, `order_validators`, the
    outlet PUT and the sales visit order. A reschedule maps the codes instead
    (F10). `now_local` is injected rather than read so the caller owns the
    clock and tests can freeze it.
    """
    return [
        _SCHEDULE_ERROR_MESSAGES[code]
        for code in schedule_error_codes(delivery_date, window_start, window_end, now_local=now_local)
    ]


def local_now() -> datetime:
    """The current moment in the timezone the business actually operates in.

    The ONE definition of which clock `today` means. Resolved here rather than
    taken from each caller, because a caller that passes a UTC `now` gets a
    `today` that is a day behind for the five hours between 19:00Z and local
    midnight — long enough for the horizon to move under an operator mid-shift.
    """
    return datetime.now(ZoneInfo(DISPLAY_TIMEZONE))


def _coerce_date(raw: Any) -> Optional[date]:
    """Accept what a request actually carries: an ISO string (raw JSON body) or
    an already-parsed `date` (a Pydantic-validated request model)."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    return date.fromisoformat(str(raw).strip())  # raises ValueError on anything else


def parse_schedule(
    raw_date: Any,
    raw_window_start: Any,
    raw_window_end: Any,
) -> Tuple[Optional[date], Optional[time], Optional[time]]:
    """Turn a request's three raw schedule values into typed ones, and judge nothing.

    The ONE parser. `parse_and_validate_schedule` is this plus `validate_schedule`.
    The reschedule PATCH calls it alone, because `OrderScheduleService.reschedule`
    owns the reschedule's rule (F10). Blank and None mean "not set".

    Raises ValueError, worded "Invalid delivery schedule: ...", for anything
    malformed, a value of the wrong type included. A caller then catches one
    exception type and answers 400, never 500.
    """
    try:
        return _coerce_date(raw_date), parse_window_time(raw_window_start), parse_window_time(raw_window_end)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid delivery schedule: {exc}") from exc


def parse_and_validate_schedule(
    raw_date: Any,
    raw_window_start: Any,
    raw_window_end: Any,
    *,
    now_local: Optional[datetime] = None,
) -> Tuple[Optional[date], Optional[time], Optional[time], List[str]]:
    """Turn a request's three raw schedule values into a validated schedule.

    `parse_schedule`, then `validate_schedule`, for the write paths that answer
    with sentences: admin create-order, web checkout and the sales visit order.
    The reschedule PATCH parses with `parse_schedule` alone. Its rule lives in
    `OrderScheduleService.reschedule`, which answers with codes (F10). So the
    parser exists once, and so does the rule.

    Returns `(delivery_date, window_start, window_end, errors)`. A non-empty
    `errors` list means NOTHING was accepted and the caller must reject the
    request. Never raises — a malformed value is a 400-worthy error string, not
    a 500.

    `now_local` defaults to `local_now()`: choosing the clock is not the
    caller's job, and a caller that got it wrong would silently validate
    against a `today` a day behind. Injectable so tests can freeze it.
    """
    if now_local is None:
        now_local = local_now()

    try:
        delivery_date, window_start, window_end = parse_schedule(raw_date, raw_window_start, raw_window_end)
    except ValueError as exc:
        return None, None, None, [str(exc)]

    return (
        delivery_date,
        window_start,
        window_end,
        validate_schedule(delivery_date, window_start, window_end, now_local=now_local),
    )
