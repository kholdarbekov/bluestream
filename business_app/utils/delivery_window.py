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


def validate_schedule(
    delivery_date: Optional[date],
    window_start: Optional[time],
    window_end: Optional[time],
    *,
    now_local: datetime,
) -> List[str]:
    """Validate a requested schedule. Empty list == valid.

    Shared by every write path (admin create, web checkout, reschedule) so the
    rule cannot drift between them. `now_local` is injected rather than read so
    the caller owns the clock and tests can freeze it.
    """
    errors: List[str] = []
    today = now_local.date()

    if delivery_date is not None:
        if delivery_date < today:
            errors.append("delivery_date cannot be in the past")
        elif delivery_date > today + timedelta(days=MAX_SCHEDULE_HORIZON_DAYS):
            errors.append(f"delivery_date cannot be more than {MAX_SCHEDULE_HORIZON_DAYS} days in the future")

    if window_start is not None and window_end is not None and window_start >= window_end:
        errors.append("window_start must be before window_end")

    # An "until 10:00" order placed at 14:00 today is a promise that cannot be
    # kept. Only the END is checked: "after 19:00" today at 20:00 is merely late,
    # not impossible.
    if delivery_date is not None and delivery_date == today and window_end is not None:
        if window_end <= now_local.time():
            errors.append("delivery window has already passed for today")

    return errors


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


def parse_and_validate_schedule(
    raw_date: Any,
    raw_window_start: Any,
    raw_window_end: Any,
    *,
    now_local: Optional[datetime] = None,
) -> Tuple[Optional[date], Optional[time], Optional[time], List[str]]:
    """Turn a request's three raw schedule values into a validated schedule.

    The ONE entry point every HTTP write path uses (admin create-order, web
    checkout, and any later reschedule), so no endpoint can parse or validate a
    schedule its own way: a rule expressed twice is a rule that drifts.

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
        delivery_date = _coerce_date(raw_date)
        window_start = parse_window_time(raw_window_start)
        window_end = parse_window_time(raw_window_end)
    except (AttributeError, TypeError, ValueError) as exc:
        return None, None, None, [f"Invalid delivery schedule: {exc}"]

    return (
        delivery_date,
        window_start,
        window_end,
        validate_schedule(delivery_date, window_start, window_end, now_local=now_local),
    )
