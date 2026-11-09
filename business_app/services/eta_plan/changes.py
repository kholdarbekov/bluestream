"""The §4.9 rule: when a moved promise is worth a message to the customer (honest-ETA Phase 2 plan, Task 9).

Phase 2 sends nothing. The accuracy report counts the messages the rule WOULD send over the engine's rows, so the owner
sees how often a customer would hear from us before Phase 3 sends them.

- What is a change (`move_message_reason`), compared with the last promise the customer would have been told:
  - "day": the promised day changed (R11's `promised_date`, the local date of E). A window end crossing midnight with E
    on the same date is not a new day, and nor is midnight alone, when the label turns from "tomorrow" to "today";
  - "later": the window end moved ETA_CHANGE_LATER_MINUTES or more later;
  - "earlier": it moved ETA_CHANGE_EARLIER_MINUTES or more earlier (good news).
  A change of day wins over a move of the end: it is the bigger news, and one message tells both.
- What the customer was told (`messages_that_would_send`). The baseline is the delivery's first engine row with a
  window, then each row that would have sent a message. Rows before the first window (unconfirmed, or a date without
  a time) tell no time, so the first window is the first promise, not a change. A change the limits hold back does not
  move the baseline, so small moves add up.
- The limits: at most one message per ETA_CHANGE_MIN_GAP_MINUTES and ETA_CHANGE_MAX_PER_ORDER per order, and none in
  the quiet hours, from ETA_QUIET_START_MINUTES to ETA_QUIET_END_MINUTES past local midnight. A change the limits hold
  goes out at the first moment they allow, the end of the hour or 08:00, if it still holds against the baseline then:
  the promise at that moment is the latest row before it. That is what Phase 3's sender does when the beat re-reads the
  promise; the spec names the 08:00 case.

Instants in and out are aware UTC; the tunables are read from `current_app.config`.
"""

from datetime import datetime, timedelta
from typing import List, NamedTuple, Optional, Sequence

from flask import current_app

from business_app.models.delivery_promise import DeliveryPromise
from business_app.services.eta_plan.common import minutes_between
from business_app.utils.local_windows import local_date, local_instant_at_minutes, minutes_past_local_midnight
from business_app.utils.timezone_utils import ensure_utc

LATER = "later"
EARLIER = "earlier"
DAY = "day"


class ChangeMessage(NamedTuple):
    """One message the rule would send: when, and why ("later", "earlier" or "day")."""

    at: datetime
    reason: str


class _Held(NamedTuple):
    """A change the limits hold: the row it came from, its reason, and the first moment it may go out."""

    row: DeliveryPromise
    reason: str
    release_at: datetime


def move_message_reason(baseline: DeliveryPromise, new: DeliveryPromise) -> Optional[str]:
    """Why `new` is worth a message against `baseline`, the last promise the customer would have been told: "day",
    "later" or "earlier", or None when it is not."""
    if new.promised_date != baseline.promised_date:
        return DAY
    if baseline.window_end is None or new.window_end is None:
        return None
    config = current_app.config
    moved = minutes_between(ensure_utc(baseline.window_end), ensure_utc(new.window_end))
    if moved >= config["ETA_CHANGE_LATER_MINUTES"]:
        return LATER
    if moved <= -config["ETA_CHANGE_EARLIER_MINUTES"]:
        return EARLIER
    return None


def messages_that_would_send(
    rows: Sequence[DeliveryPromise], *, until: Optional[datetime] = None
) -> List[ChangeMessage]:
    """The messages the rule would send over one delivery's engine rows, given oldest first (by `computed_at`, then
    id). `until` ends the waiting stage (the report passes now, or the arrival): a change still held then is not
    sent. With None, a held change is sent when the limits allow it."""
    most = current_app.config["ETA_CHANGE_MAX_PER_ORDER"]
    sent: List[ChangeMessage] = []
    baseline: Optional[DeliveryPromise] = None
    held: Optional[_Held] = None
    for row in rows:
        computed_at = ensure_utc(row.computed_at)
        if baseline is None:
            if row.window_end is not None:
                baseline = row
            continue
        if held is not None and held.release_at < computed_at:
            # Nothing was recorded between the change and its release: it still held, and went out then.
            sent.append(ChangeMessage(held.release_at, held.reason))
            baseline, held = held.row, None
        reason = move_message_reason(baseline, row)
        if reason is None or len(sent) >= most:
            held = None
            continue
        release_at = _first_allowed(computed_at, sent[-1].at if sent else None)
        if release_at == computed_at:
            sent.append(ChangeMessage(computed_at, reason))
            baseline, held = row, None
        else:
            held = _Held(row, reason, release_at)
    if held is not None and (until is None or held.release_at < ensure_utc(until)):
        sent.append(ChangeMessage(held.release_at, held.reason))
    return sent


def _first_allowed(at: datetime, last_sent: Optional[datetime]) -> datetime:
    """The first moment from `at` a message may go out: an hour after the last one, and outside the quiet hours."""
    config = current_app.config
    if last_sent is not None:
        at = max(at, last_sent + timedelta(minutes=config["ETA_CHANGE_MIN_GAP_MINUTES"]))
    if not _quiet(at):
        return at
    day = local_date(at)
    quiet_end = local_instant_at_minutes(day, config["ETA_QUIET_END_MINUTES"])
    if quiet_end <= at:  # the quiet hours began this evening: they end tomorrow
        quiet_end = local_instant_at_minutes(day + timedelta(days=1), config["ETA_QUIET_END_MINUTES"])
    return quiet_end


def _quiet(at: datetime) -> bool:
    """Whether `at` falls in the quiet hours, from ETA_QUIET_START_MINUTES (inclusive) to ETA_QUIET_END_MINUTES past
    local midnight; the span may run over midnight, as 22:00 to 08:00 does."""
    config = current_app.config
    start, end = config["ETA_QUIET_START_MINUTES"], config["ETA_QUIET_END_MINUTES"]
    minutes = minutes_past_local_midnight(at, local_date(at))
    if start <= end:
        return start <= minutes < end
    return minutes >= start or minutes < end
