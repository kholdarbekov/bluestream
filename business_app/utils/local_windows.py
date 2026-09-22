"""Local calendar windows — the ONE place a reporting period becomes UTC instants.

Every window phase 3 reports on is a LOCAL calendar range: "today", "this week",
"01-16 September", "last Monday to Sunday". The rows it counts are stored in UTC,
so each of those has to become a half-open `[start_utc, end_utc)` pair exactly
once. Before this module the repo had a single local-midnight expression
(`route_optimization_service._driver_day_start_utc`, reached through
`ReplenishmentService.local_day_end_utc`) and no local WEEK or MONTH at all —
which is how a metrics service, a bot stats card and a weekly email would have
grown three of them and disagreed for the five hours between 19:00Z and local
midnight, every single evening.

The clock is `local_now()`'s: `business_app/utils/delivery_window.py` calls it
"the ONE definition of which clock `today` means", and only its TZINFO is
borrowed here (the digest's `_local_date` precedent). The INSTANT is always the
caller's own `now`, because a report is about one moment and must not re-read the
wall clock halfway through building itself.

Pure apart from `parse_date_range`, which reads `SALES_METRICS_MAX_RANGE_DAYS`
from the app config: parsing is where a request's range is refused, and the
ceiling is a deployable tunable rather than a literal in a route.
"""

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Tuple

from flask import current_app

from business_app.utils.delivery_window import local_now
from business_app.utils.exceptions import ValidationError
from business_app.utils.timezone_utils import ensure_utc

# The windows `GET /staff/sales/me/stats` answers for, in the order the bot draws
# its period switch. Published as DATA so the bot's tuple is pinned against this
# one (tests/unit/test_sales_visit_bot_plumbing.py) instead of restating it: the
# bot cannot import from business_app, and a hand-copied tuple is exactly where
# a renamed period rots into a screen that silently shows the wrong week.
STATS_PERIODS = ("today", "week", "month")

# One code for every way a date range can be unanswerable. The admin UI renders
# the message; the staff bot never reaches it (its periods are fixed buttons).
DATE_RANGE_INVALID = "SALES_DATE_RANGE_INVALID"


def _tz():
    """The business clock's tzinfo, borrowed from `local_now()` so this module
    does not plant a second `ZoneInfo(DISPLAY_TIMEZONE)` (and a duplicate of its
    default) beside the one the repo already calls the definition."""
    return local_now().tzinfo


def local_date(now: Optional[datetime] = None) -> date:
    """The LOCAL calendar day `now` falls in; the wall clock when `now` is None.

    A naive `now` is UTC, which is how a DateTime column round-trips out of
    SQLite — normalising here rather than at each call site is the same reason
    `ensure_utc` exists.
    """
    if now is None:
        return local_now().date()
    return ensure_utc(now).astimezone(_tz()).date()


def local_day_bounds(day: date) -> Tuple[datetime, datetime]:
    """`[start, end)` in UTC for one local calendar day.

    Half-open deliberately: consecutive days must tile without sharing a second,
    or a visit started at exactly local midnight is counted twice.
    """
    tz = _tz()
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def window_bounds(start_date: date, end_date: date) -> Tuple[datetime, datetime]:
    """`[start, end)` in UTC for an INCLUSIVE local date range."""
    return local_day_bounds(start_date)[0], local_day_bounds(end_date)[1]


def days_inclusive(start_date: date, end_date: date) -> int:
    """How many local calendar days the inclusive range covers — never below 1.

    This is `visits_per_day`'s divisor. There is no working-day calendar anywhere
    in the repo, so "days" means calendar days and an inverted range raises here
    rather than producing a negative rate three layers up.
    """
    if end_date < start_date:
        raise ValueError(f"end_date {end_date} is before start_date {start_date}")
    return (end_date - start_date).days + 1


def period_window(period: str, *, now: Optional[datetime] = None) -> Tuple[date, date]:
    """The inclusive local date range one `STATS_PERIODS` name means.

    `week` and `month` are to-DATE, not whole calendar periods: the card divides
    by the days that have happened, and an agent reading it at 10:00 on Monday
    must not see their morning averaged over seven days.
    """
    today = local_date(now)
    if period == "today":
        return today, today
    if period == "week":
        return today - timedelta(days=today.weekday()), today
    if period == "month":
        return today.replace(day=1), today
    raise ValueError(f"unknown period: {period!r}")


def resolve_stats_period(period: Optional[str]) -> Tuple[str, date, date]:
    """`?period=` on `GET /staff/sales/me/stats` -> (period, start_date, end_date).

    `period_window` answers an unknown period with `ValueError` -- the right shape for
    a library call and the wrong one for the wire, where an uncoded 400 reaches the
    staff bot as the generic "Something went wrong". Translating it one line below the
    function that raises it keeps the whole window vocabulary in one module, and it
    keeps the DEFAULT in one place too: an absent (or empty) `period` is `today`,
    decided once rather than once per caller.
    """
    resolved = period or STATS_PERIODS[0]
    try:
        start_date, end_date = period_window(resolved)
    except ValueError as exc:
        raise ValidationError(
            "Unknown stats period",
            error_code="SALES_STATS_PERIOD_INVALID",
            details={"period": period, "allowed": list(STATS_PERIODS)},
        ) from exc
    return resolved, start_date, end_date


def previous_local_week(now: Optional[datetime] = None) -> Tuple[date, date]:
    """Last week, Monday..Sunday — the window the Monday-morning report covers."""
    today = local_date(now)
    this_monday = today - timedelta(days=today.weekday())
    return this_monday - timedelta(days=7), this_monday - timedelta(days=1)


def parse_date_range(
    start_date: Optional[str],
    end_date: Optional[str],
    *,
    now: Optional[datetime] = None,
    default_days: int = 7,
) -> Tuple[date, date]:
    """`?start_date=&end_date=` -> an inclusive local date range, or one 400.

    Parsing lives HERE, not in a route: `admin_sales.py` and `staff_sales.py`
    carry an API-boundary budget of 0, and every phase-3 read route asks the same
    question — a per-route `date.fromisoformat` would be five copies of one rule
    and five different answers to one typo.

    Absent means "the last `default_days` local days, ending today", and a
    half-given range fills the other end the same way, because an operator who
    types only a start means "since then", not "no data".
    """
    today = local_date(now)
    parsed = {}
    for name, raw in (("start_date", start_date), ("end_date", end_date)):
        if raw is None or str(raw).strip() == "":
            parsed[name] = None
            continue
        try:
            parsed[name] = date.fromisoformat(str(raw).strip())
        except ValueError:
            raise ValidationError(f"{name} must be an ISO date (YYYY-MM-DD)", error_code=DATE_RANGE_INVALID)
    end = parsed["end_date"] or today
    start = parsed["start_date"] or end - timedelta(days=default_days - 1)
    if end < start:
        raise ValidationError("end_date is before start_date", error_code=DATE_RANGE_INVALID)
    # A ceiling, never a silent clamp: a clamped answer is a number an owner would
    # read as the whole period and act on.
    max_days = int(current_app.config["SALES_METRICS_MAX_RANGE_DAYS"])
    if days_inclusive(start, end) > max_days:
        raise ValidationError(f"date range is longer than {max_days} days", error_code=DATE_RANGE_INVALID)
    return start, end
