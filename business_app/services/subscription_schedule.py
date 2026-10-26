"""The subscription schedule: the ONE place that knows cadence, anchors and dates.

Spec: docs/superpowers/specs/2026-10-23-subscription-single-frequency-design.md §4.2 and §9.
Every path that dates a subscription delivery asks this module: create, the daily run, skip, pause and
resume, frequency/day/slot changes, preview, stats and admin. Nothing else does date maths for a
subscription — not JS, not the bots, not admin, not reports.

Pure: no DB, no Flask app context, no wall clock. "Today" is always handed in (the business clock is
`business_app.utils.local_windows.local_date()`). A subscription is anything with `frequency`,
`delivery_day_of_week`, `delivery_day_of_month` and `delivery_time_slot`.

Anchors (`anchor_dates`), the days a delivery may land on:
    weekly, biweekly  `delivery_day_of_week`, 0 = Monday (Python's `weekday()`)
    monthly           `delivery_day_of_month`, clamped to the month's last day
    daily             the weekdays the slot is available on (`DeliveryTimeSlot.available_days`)
A day that is not set anchors on every day. So does a daily subscription with no slot, an inactive
slot or a slot offering no day: those mean "dated, time to be confirmed" (§6.6), never "no delivery".

Cadence (`advance`): one cycle (1 day, 7 days, 14 days or one calendar month) after the last delivery,
then the first anchor on or after that. Biweekly therefore keeps the parity of its last delivery with
no extra column (§9). A monthly on the 31st lands on 28/29 February and 30 April and returns to the
31st when the month has one. A changed day never gives two deliveries back to back: the next one is
the first new anchor on or after the last delivery plus one full cycle (§9).
"""

from calendar import monthrange
from datetime import date, time, timedelta
from decimal import Decimal
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from dateutil.relativedelta import relativedelta

from business_app.utils.delivery_window import parse_window_time
from shared.business_config import SUBSCRIPTION_ORDER_LEAD_DAYS
from shared.enums import SubscriptionFrequency

_ONE_DAY = timedelta(days=1)
_WEEKDAYS = range(7)
_MONTH_DAYS = range(1, 32)

_CYCLE = {
    SubscriptionFrequency.DAILY: relativedelta(days=1),
    SubscriptionFrequency.WEEKLY: relativedelta(weeks=1),
    SubscriptionFrequency.BIWEEKLY: relativedelta(weeks=2),
    SubscriptionFrequency.MONTHLY: relativedelta(months=1),
}

# Calendar-accurate (§4.2): the preview, customer stats, the admin's monthly revenue, the bot and
# the web all read this one number.
_DELIVERIES_PER_MONTH = {
    SubscriptionFrequency.DAILY: Decimal(365) / Decimal(12),
    SubscriptionFrequency.WEEKLY: Decimal(52) / Decimal(12),
    SubscriptionFrequency.BIWEEKLY: Decimal(26) / Decimal(12),
    SubscriptionFrequency.MONTHLY: Decimal(1),
}


def _frequency(value) -> SubscriptionFrequency:
    return value if isinstance(value, SubscriptionFrequency) else SubscriptionFrequency(value)


def _slot_weekdays(slot) -> Optional[frozenset]:
    """The weekdays a daily delivery may land on, or None for every day."""
    if slot is None or not slot.is_active:
        return None
    return frozenset(day for day in (slot.available_days or ()) if day in _WEEKDAYS) or None


def _anchor_rule(subscription) -> Callable[[date], bool]:
    """The predicate "a delivery may land on this day", checked once so no search can run forever:
    every rule it returns is true at least once in any 31 consecutive days."""
    frequency = _frequency(subscription.frequency)
    if frequency in (SubscriptionFrequency.WEEKLY, SubscriptionFrequency.BIWEEKLY):
        weekday = subscription.delivery_day_of_week
        if weekday is None:
            return lambda day: True
        if weekday not in _WEEKDAYS:
            raise ValueError(f"delivery_day_of_week must be 0-6 (0 = Monday), not {weekday!r}")
        return lambda day: day.weekday() == weekday
    if frequency == SubscriptionFrequency.MONTHLY:
        month_day = subscription.delivery_day_of_month
        if month_day is None:
            return lambda day: True
        if month_day not in _MONTH_DAYS:
            raise ValueError(f"delivery_day_of_month must be 1-31, not {month_day!r}")
        return lambda day: day.day == min(month_day, monthrange(day.year, day.month)[1])
    weekdays = _slot_weekdays(subscription.delivery_time_slot)
    if weekdays is None:
        return lambda day: True
    return lambda day: day.weekday() in weekdays


def anchor_dates(subscription, start: date) -> Iterator[date]:
    """Every date on or after `start` that the subscription's day and slot allow, in order (endless)."""
    is_anchor = _anchor_rule(subscription)
    day = start
    while True:
        if is_anchor(day):
            yield day
        day += _ONE_DAY


def first_delivery_on_or_after(subscription, day: date) -> date:
    return next(anchor_dates(subscription, day))


def advance(subscription, from_date: date) -> date:
    """The delivery one cycle after `from_date` (normally the last delivery)."""
    return first_delivery_on_or_after(subscription, from_date + _CYCLE[_frequency(subscription.frequency)])


def earliest_delivery_date(today: date) -> date:
    """The soonest a new subscription's first delivery, or the first after a resume, may land."""
    return today + timedelta(days=SUBSCRIPTION_ORDER_LEAD_DAYS)


def due_through(today: date) -> date:
    """The last delivery date whose order is created by `today`: each order is created the lead before
    its delivery (§4.3). The same day as `earliest_delivery_date`, so a new subscription's first order
    is due the day the subscription is created, at the soonest."""
    return earliest_delivery_date(today)


def first_delivery(subscription, today: date, start: Optional[date] = None) -> date:
    """A new subscription's first delivery (from its `start` day) or the first after a resume: the first
    anchor on or after the later of `start` and the soonest the lead allows (§4.3, §4.6)."""
    soonest = earliest_delivery_date(today)
    return first_delivery_on_or_after(subscription, max(start, soonest) if start is not None else soonest)


def missing_anchor_day(subscription, first_delivery: date) -> Dict[str, int]:
    """The delivery day a weekly, biweekly or monthly subscription was created without, fixed from its first
    delivery: that date's weekday (0 = Monday) or day of the month, as the migration fixed old rows (§2.9).
    Empty when the day is set, or for daily, which has none. The first delivery stays one of its days."""
    frequency = _frequency(subscription.frequency)
    if frequency in (SubscriptionFrequency.WEEKLY, SubscriptionFrequency.BIWEEKLY):
        return (
            {} if subscription.delivery_day_of_week is not None else {"delivery_day_of_week": first_delivery.weekday()}
        )
    if frequency == SubscriptionFrequency.MONTHLY:
        return {} if subscription.delivery_day_of_month is not None else {"delivery_day_of_month": first_delivery.day}
    return {}


def catch_up(subscription, day: date, today: date) -> Tuple[date, List[date]]:
    """Never back-date (§2.8): every scheduled date before `today`, from `day` on, is skipped. Returns the
    first scheduled date on or after `today`, reached on the subscription's own cadence, and the skipped
    dates (empty when `day` has not passed)."""
    skipped = []
    while day < today:
        skipped.append(day)
        day = advance(subscription, day)
    return day, skipped


def delivery_after_change(subscription, today: date, last_delivery: Optional[date], *, order_created: bool) -> date:
    """The next delivery after the frequency, day or slot changed, under the NEW settings (§4.6, §9).

    Never sooner than the lead allows, and never on the day of the last delivery. When that delivery's
    order is already created it is kept, so the next one waits a full new cycle after it: a change never
    gives two deliveries back to back."""
    soonest = earliest_delivery_date(today)
    if last_delivery is None:
        return first_delivery_on_or_after(subscription, soonest)
    if order_created:
        return max(advance(subscription, last_delivery), first_delivery_on_or_after(subscription, soonest))
    return first_delivery_on_or_after(subscription, max(soonest, last_delivery + _ONE_DAY))


def validate_next_delivery(subscription, day: date, today: date) -> None:
    """Raise ValueError unless `day` may be the subscription's next delivery: one of its delivery days, no
    sooner than the lead allows. Admin "Set next delivery date" goes through this (§9)."""
    soonest = earliest_delivery_date(today)
    if day < soonest:
        raise ValueError(f"The next delivery cannot be before {soonest.isoformat()}")
    if first_delivery_on_or_after(subscription, day) != day:
        raise ValueError(f"{day.isoformat()} is not one of this subscription's delivery days")


def deliveries_per_month(frequency) -> Decimal:
    return _DELIVERIES_PER_MONTH[_frequency(frequency)]


def window_for(slot, day: date) -> Optional[Tuple[time, time]]:
    """The slot's window on `day`, or None when there is no slot, it is inactive, or it is not offered
    that weekday; the order is then dated without a window. The slot's fees are never applied:
    delivery is always free and `create_order` keeps its own fee rule (§4.2)."""
    if slot is None or not slot.is_active or day.weekday() not in (slot.available_days or ()):
        return None
    return parse_window_time(slot.start_time), parse_window_time(slot.end_time)
