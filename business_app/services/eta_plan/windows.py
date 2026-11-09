"""Windows: from the plan's expected arrival E to the window a customer is promised (honest-ETA Phase 2 plan, R11).

The raw window is `[E + lo, E + hi]`. Every rule that turns it into what is recorded lives here, once, for the planner
(which compares candidate placements by their recorded window end) and the recorder (which writes it):

- `lo_hi`: lo and hi come from the fit's bucket for (stops ahead in the car's plan, whether the car has started), in
  five bands. A bucket with fewer than ETA_WINDOW_MIN_PROMISES fitted promises borrows the next band up with the same
  started flag, cascading to "8+", and then the band's own seed (spec 4.6). Only seeds get the extra hour on hi for a
  car that has not started: a fitted not-started bucket already holds that spread. hi then never falls as the band
  grows, so a stop with more ahead never reads a narrower window, which is what lets a second car only shorten a
  promise.
- `display_window`: the window rounded outward to ETA_WINDOW_ROUND_MINUTES on the LOCAL clock, never starting before
  now. "By-only" (wider than ETA_WINDOW_BY_ONLY_HOURS) is derived from the stored window when it is read (`by_only`).
- `customer_bounded_window`: a customer's own window ("between", "until", "after") wins whenever the plan meets it; a
  plan that misses its end records the computed window, which is the honest later one.
- `day_kind` and `promised_date`: the day the promise names, on the customer's calendar, not the plan day (05:00 to
  05:00 local), so at 02:00 a promise for 16:00 is "today". Both read the very same instant, E.

Every instant in and out is aware UTC. The tunables are read from `current_app.config`.
"""

from datetime import date, datetime, timedelta
from typing import Optional, Tuple

from flask import current_app

from business_app.models.delivery_promise import DAY_KIND_DATE, DAY_KIND_TODAY, DAY_KIND_TOMORROW
from business_app.services.eta_plan.params import EtaParams
from business_app.utils.local_windows import local_date, work_day_bounds, work_day_of
from business_app.utils.timezone_utils import utc_to_local
from shared import business_config
from shared.constants import DISPLAY_TIMEZONE

# The bands of "stops ahead of this one in its car's plan" that windows are bucketed by (spec 4.6).
STOPS_AHEAD_BANDS = ("0", "1-2", "3-4", "5-7", "8+")
# The most stops ahead each band but the last holds; "8+" takes the rest.
_MOST_AHEAD = (0, 2, 4, 7)


def band_of(stops_ahead: int) -> str:
    """The band a stop with `stops_ahead` ahead of it belongs to: "0", "1-2", "3-4", "5-7" or "8+"."""
    for band, most_ahead in zip(STOPS_AHEAD_BANDS, _MOST_AHEAD):
        if stops_ahead <= most_ahead:
            return band
    return STOPS_AHEAD_BANDS[-1]


def _key(band: str, started: bool) -> str:
    return f"{band}|{'started' if started else 'not_started'}"


def bucket_key(stops_ahead: int, started: bool) -> str:
    """The key of a window bucket in `EtaParams.window_buckets`: `"1-2|started"`, `"0|not_started"`."""
    return _key(band_of(stops_ahead), started)


def _band_lo_hi(params: EtaParams, band: str, started: bool) -> Tuple[int, int, bool]:
    """One band's lo and hi before the envelope, and whether they are the seed's.

    The band's own bucket when it has enough promises, else the next band up's (cascading to "8+"), else the band's
    own seed, with the extra hour on hi when the car has not started.
    """
    config = current_app.config
    for candidate in STOPS_AHEAD_BANDS[STOPS_AHEAD_BANDS.index(band) :]:
        bucket = params.window_buckets.get(_key(candidate, started))
        if (
            bucket is not None
            and bucket["n"] >= config["ETA_WINDOW_MIN_PROMISES"]
            and bucket["lo"] is not None
            and bucket["hi"] is not None
        ):
            return bucket["lo"], bucket["hi"], False
    lo, hi = business_config.ETA_WINDOW_SEEDS[band]
    if not started:
        hi += config["ETA_NOT_STARTED_EXTRA_HI_MINUTES"]
    return lo, hi, True


def lo_hi(params: EtaParams, stops_ahead: int, started: bool) -> Tuple[int, int, bool]:
    """`(lo, hi, seeded)`: the minutes around E that the window spans, for a stop with `stops_ahead` ahead of it.

    `seeded` is True when the band's own lo and hi are the seed's, not a fit's. hi is lifted to the largest hi of this
    band and every band below it with the same started flag, each read as above; lo is not lifted.
    """
    band = band_of(stops_ahead)
    lo, hi, seeded = _band_lo_hi(params, band, started)
    for lower in STOPS_AHEAD_BANDS[: STOPS_AHEAD_BANDS.index(band)]:
        hi = max(hi, _band_lo_hi(params, lower, started)[1])
    return lo, hi, seeded


def _round_to_step(instant: datetime, *, up: bool) -> datetime:
    """`instant` rounded down, or up, to a multiple of ETA_WINDOW_ROUND_MINUTES past LOCAL midnight.

    The step is counted on the local wall clock, never from UTC midnight: the two agree only while the offset is a whole
    number of steps, which no setting of the display timezone or the step guarantees.
    """
    step = timedelta(minutes=current_app.config["ETA_WINDOW_ROUND_MINUTES"])
    local = utc_to_local(instant, DISPLAY_TIMEZONE)
    past_midnight = timedelta(
        hours=local.hour, minutes=local.minute, seconds=local.second, microseconds=local.microsecond
    )
    over = past_midnight % step
    if not over:
        return instant
    return instant + (step - over if up else -over)


def display_window(expected: datetime, lo: int, hi: int, now: datetime) -> Tuple[datetime, datetime]:
    """`[E + lo, E + hi]` rounded outward on the local clock, starting no earlier than `now`.

    A window that would end at or before its start (E + hi already in the past) ends at the next step after its start.
    """
    start = max(now, _round_to_step(expected + timedelta(minutes=lo), up=False))
    end = _round_to_step(expected + timedelta(minutes=hi), up=True)
    if end <= start:
        end = _round_to_step(start + timedelta(minutes=1), up=True)
    return start, end


def customer_bounded_window(
    expected: datetime,
    lo: int,
    hi: int,
    now: datetime,
    window_start: Optional[datetime],
    window_end: Optional[datetime],
) -> Tuple[datetime, datetime]:
    """The window to record for a stop whose customer chose `window_start` and `window_end` (each None when open).

    - "Between" (both): when E is at or before the customer's end, their own window, from now at the earliest.
    - "Until" (end only): the same, from the start of E's plan day (05:00 local) or now, whichever is later.
    - "After" (start only): the computed window, starting no earlier than the customer's time.
    - E past the customer's end, or no window at all: the computed window (`display_window`). A plan that misses the
      end records the honest later window rather than the customer's own.

    The plan never puts a windowed stop before its window starts (the driver waits), so E at or before the end means the
    plan meets the window.
    """
    if window_end is None:
        return display_window(expected, lo, hi, now if window_start is None else max(now, window_start))
    if expected > window_end:
        return display_window(expected, lo, hi, now)
    if window_start is None:
        return max(now, work_day_bounds(work_day_of(expected))[0]), window_end
    return max(now, window_start), window_end


def by_only(window_start: datetime, window_end: datetime) -> bool:
    """Whether a recorded window is shown as "by HH:MM", its end only: wider than ETA_WINDOW_BY_ONLY_HOURS (spec §10,
    call 2). Derived when the window is read, never stored. Such a window tells the customer no start."""
    return window_end - window_start > timedelta(hours=current_app.config["ETA_WINDOW_BY_ONLY_HOURS"])


def day_kind(expected: datetime, now: datetime) -> str:
    """The label of the promised day on the customer's calendar: "today", "tomorrow" or "date".

    The local calendar date of E against that of `now`. E is never before `now`, so a date that is not later is today.
    """
    days_ahead = (local_date(expected) - local_date(now)).days
    if days_ahead <= 0:
        return DAY_KIND_TODAY
    return DAY_KIND_TOMORROW if days_ahead == 1 else DAY_KIND_DATE


def promised_date(expected: Optional[datetime], customer_date: Optional[date]) -> Optional[date]:
    """The local calendar date of E, so a window that runs past midnight does not change it.

    A dated row without a time promises the customer's date, and a row with neither promises none.
    """
    if expected is None:
        return customer_date
    return local_date(expected)
