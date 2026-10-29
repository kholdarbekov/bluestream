"""The subscription schedule: the ONE place that knows cadence, anchors and dates.

Spec: docs/superpowers/specs/2026-10-23-subscription-single-frequency-design.md §4.2 and §9; delivery days:
docs/superpowers/specs/2026-10-27-bot-web-parity-design.md §6.4 and §17 Q7.
Every path that dates a subscription delivery asks this module: create, the daily run, skip, pause and
resume, frequency/day/slot changes, preview, stats and admin. Nothing else does date maths for a
subscription — not JS, not the bots, not admin, not reports.

No Flask app context and no wall clock: "today" is always handed in (the business clock is
`business_app.utils.local_windows.local_date()`). A subscription is anything with `frequency`,
`delivery_day_of_week`, `delivery_day_of_month` and `delivery_time_slot`. One database read:
`delivery_weekdays()`, the delivery days, read at most once per public call and only by a rule that needs it
(a caller over many rows may read it once and hand it down as `delivery_days`).

Delivery days (§6.4): the weekdays at least one ACTIVE slot is offered on. With no slot defined, or every
slot switched off, every day is a delivery day, so no schedule can stall (§17 Q7).

Anchors (`anchor_dates`), the days a delivery may land on:
    weekly, biweekly  `delivery_day_of_week`, 0 = Monday (Python's `weekday()`)
    monthly           `delivery_day_of_month`, clamped to the month's last day; on a day that is not a delivery
                      day it moves to the next delivery day, and still counts as that month's date
    daily             the weekdays the slot is available on (`DeliveryTimeSlot.available_days`)
A day that is not set anchors on every delivery day. So does a daily subscription with no slot ("Any time"),
an inactive slot or a slot offering no day ("Time to be confirmed"): never "no delivery".

Cadence (`advance`): one cycle (1 day, 7 days, 14 days or one calendar month) after the last delivery,
then the first anchor on or after that. Biweekly therefore keeps the parity of its last delivery with
no extra column (§9). A monthly on the 31st lands on 28/29 February and 30 April and returns to the
31st when the month has one; a moved monthly delivery counts its month from its own date, not the day it
moved to, even after a delivery day is added or a slot write moved it on again, so no month is skipped. A changed
day never gives two deliveries back to back: the next one is the first new anchor on or after the last delivery
plus one full cycle (§9).
Every search for a date ends within `_SEARCH_DAYS`.
"""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, time, timedelta
from decimal import Decimal
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from dateutil.relativedelta import relativedelta

from business_app.utils.delivery_window import parse_window_time
from shared.business_config import SUBSCRIPTION_ORDER_LEAD_DAYS
from shared.enums import SubscriptionFrequency

_ONE_DAY = timedelta(days=1)
_WEEKDAYS = range(7)
_MONTH_DAYS = range(1, 32)
# The longest any search for a date may run: one cycle (at most a month, 31 days, with its clamp) plus the move
# to the next delivery day, with room to spare. Every rule holds at least once in that many days.
_SEARCH_DAYS = 62

_CYCLE = {
    SubscriptionFrequency.DAILY: relativedelta(days=1),
    SubscriptionFrequency.WEEKLY: relativedelta(weeks=1),
    SubscriptionFrequency.BIWEEKLY: relativedelta(weeks=2),
    SubscriptionFrequency.MONTHLY: relativedelta(months=1),
}

# Calendar-accurate (§4.2): the preview, customer stats, the admin's monthly revenue, the bot and
# the web all read this one number. Daily counts its own weekdays (`deliveries_per_month`).
_DELIVERIES_PER_MONTH = {
    SubscriptionFrequency.WEEKLY: Decimal(52) / Decimal(12),
    SubscriptionFrequency.BIWEEKLY: Decimal(26) / Decimal(12),
    SubscriptionFrequency.MONTHLY: Decimal(1),
}


def _frequency(value) -> SubscriptionFrequency:
    return value if isinstance(value, SubscriptionFrequency) else SubscriptionFrequency(value)


def weekdays_offered(day_lists: Iterable[Iterable[int]]) -> frozenset:
    """The delivery days (§6.4) of these slots' `available_days`: every weekday (0-6) any of them offers, and every
    day when none offers one (§17 Q7). Values that are not a weekday are ignored."""
    offered = frozenset(day for days in day_lists for day in (days or ()) if day in _WEEKDAYS)
    return offered or frozenset(_WEEKDAYS)


def delivery_weekdays() -> frozenset:
    """The delivery days now: `weekdays_offered` over the active slots. The module's one database read."""
    from business_app.models.delivery import DeliveryTimeSlot

    rows = DeliveryTimeSlot.query.filter_by(is_active=True).with_entities(DeliveryTimeSlot.available_days).all()
    return weekdays_offered(days for (days,) in rows)


@dataclass(frozen=True)
class SlotState:
    """What a slot offers, read before a slot write changes it (spec 2026-10-27 §6.5): the slot cascade compares
    the slot's state before the write with the slot after it."""

    is_active: bool
    days: frozenset
    start_time: str
    end_time: str

    @classmethod
    def of(cls, slot) -> "SlotState":
        return cls(
            is_active=bool(slot.is_active),
            days=frozenset(day for day in (slot.available_days or ()) if day in _WEEKDAYS),
            start_time=slot.start_time,
            end_time=slot.end_time,
        )

    @property
    def weekdays(self) -> Optional[frozenset]:
        """The weekdays a daily delivery on this slot lands on, or None when it gives none (switched off, or no day
        offered): the delivery days then."""
        return self.days if self.is_active and self.days else None

    @property
    def window_label(self) -> Optional[str]:
        """The window as a notice names it, "10:00–12:00", or None when a time is missing or unreadable."""
        from business_app.utils.display_labels import window

        try:
            return window(parse_window_time(self.start_time), parse_window_time(self.end_time))
        except ValueError:
            return None


def _slot_weekdays(slot) -> Optional[frozenset]:
    """The weekdays a daily delivery on `slot` may land on, or None when the slot gives none (no slot, an inactive
    one or one offering no day): the delivery days then."""
    return None if slot is None else SlotState.of(slot).weekdays


def next_delivery_weekday(weekday: int, delivery_days: frozenset) -> int:
    """The delivery day a weekly or biweekly day moves to when it stops being one (spec 2026-10-27 §6.5): the first
    of the following seven weekdays that is a delivery day."""
    for step in range(1, len(_WEEKDAYS) + 1):
        candidate = (weekday + step) % len(_WEEKDAYS)
        if candidate in delivery_days:
            return candidate
    raise ValueError("No delivery day in the week")


def _target_on_or_before(day: date, month_day: int) -> date:
    """The latest monthly date (`month_day`, clamped to its month's last day) on or before `day`."""
    this = date(day.year, day.month, min(month_day, monthrange(day.year, day.month)[1]))
    if this <= day:
        return this
    prev = day.replace(day=1) - _ONE_DAY
    return date(prev.year, prev.month, min(month_day, monthrange(prev.year, prev.month)[1]))


class _Rule:
    """One subscription's schedule for one public call: "a delivery may land on this day" (`is_anchor`), the first
    such day on or after a date, and one cycle on (`advance`). The delivery days are read at most once, and only
    when the rule needs them: a weekly or biweekly day that is set never reads them. Every predicate holds at
    least once in any `_SEARCH_DAYS` consecutive days, so every search ends."""

    def __init__(self, subscription):
        self._frequency = _frequency(subscription.frequency)
        self._delivery_days: Optional[frozenset] = None
        self._month_day: Optional[int] = None
        self.is_anchor: Callable[[date], bool] = self._predicate(subscription)

    def delivery_days(self) -> frozenset:
        if self._delivery_days is None:
            self._delivery_days = delivery_weekdays()
        return self._delivery_days

    def _on_a_delivery_day(self, day: date) -> bool:
        return day.weekday() in self.delivery_days()

    def _predicate(self, subscription) -> Callable[[date], bool]:
        if self._frequency in (SubscriptionFrequency.WEEKLY, SubscriptionFrequency.BIWEEKLY):
            weekday = subscription.delivery_day_of_week
            if weekday is None:
                return self._on_a_delivery_day
            if weekday not in _WEEKDAYS:
                raise ValueError(f"delivery_day_of_week must be 0-6 (0 = Monday), not {weekday!r}")
            # Kept even when the day stops being a delivery day: the slot cascade moves the day itself (§6.5).
            return lambda day: day.weekday() == weekday
        if self._frequency == SubscriptionFrequency.MONTHLY:
            month_day = subscription.delivery_day_of_month
            if month_day is None:
                return self._on_a_delivery_day
            if month_day not in _MONTH_DAYS:
                raise ValueError(f"delivery_day_of_month must be 1-31, not {month_day!r}")
            self._month_day = month_day
            return self._monthly_anchor
        weekdays = _slot_weekdays(subscription.delivery_time_slot)
        if weekdays is None:
            return self._on_a_delivery_day
        return lambda day: day.weekday() in weekdays

    def _monthly_anchor(self, day: date) -> bool:
        """A delivery day with no delivery day between its month's date and it: the date itself, or the delivery
        day it moves to (§6.4)."""
        target = _target_on_or_before(day, self._month_day)
        return self._on_a_delivery_day(day) and not any(
            self._on_a_delivery_day(target + timedelta(days=n)) for n in range((day - target).days)
        )

    def first_on_or_after(self, day: date) -> date:
        for offset in range(_SEARCH_DAYS):
            candidate = day + timedelta(days=offset)
            if self.is_anchor(candidate):
                return candidate
        raise ValueError(f"No delivery day within {_SEARCH_DAYS} days of {day.isoformat()}")

    def advance(self, from_date: date, *, own_delivery: bool = True) -> date:
        """One cycle after `from_date`, then the first anchor on or after it. Always later than `from_date`.

        A monthly delivery counts its cycle from its month's date, so one moved to the next month (31 January to
        Monday 1 February) never skips that month:
        - `own_delivery` (every caller but `delivery_after_change`): a date this schedule gave is its month's, the
          latest month's date on or before it, whatever the delivery days are now. It moved under the delivery days
          of its time, and a slot write may have moved it on again (`delivery_after_days_changed`); a delivery day
          added since (a Sunday slot) must not make it a date of its own and skip the month after it. The answer is
          the next month's delivery: a month's own anchor is the first delivery day on or after its date, so no
          anchor lies between this month's and the next month's.
        - a last delivery made before a change of day or frequency (§9) counts from its month's date only when it
          is one of today's anchors, and otherwise one full cycle from itself."""
        base = from_date
        if self._month_day is not None and (own_delivery or self.is_anchor(from_date)):
            base = _target_on_or_before(from_date, self._month_day)
        return self.first_on_or_after(base + _CYCLE[self._frequency])


def anchor_dates(subscription, start: date) -> Iterator[date]:
    """Every date on or after `start` that the subscription's day and slot allow, in order (endless)."""
    rule = _Rule(subscription)
    day = start
    while True:
        day = rule.first_on_or_after(day)
        yield day
        day += _ONE_DAY


def first_delivery_on_or_after(subscription, day: date) -> date:
    """The first delivery day on or after `day`; ValueError when none comes within `_SEARCH_DAYS`."""
    return _Rule(subscription).first_on_or_after(day)


def advance(subscription, from_date: date) -> date:
    """The delivery one cycle after `from_date` (normally the last delivery)."""
    return _Rule(subscription).advance(from_date)


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
    rule = _Rule(subscription)
    skipped = []
    while day < today:
        skipped.append(day)
        day = rule.advance(day)
    return day, skipped


def clock_origin(subscription, day: Optional[date]) -> Optional[date]:
    """The scheduled date the clock last advanced from to reach `day`: the latest delivery day whose
    `advance` is `day`. That is the last delivery, or a delivery that was skipped (by the customer, or by the
    system for a refusal or a date that passed). None when no delivery day leads to `day` (a first delivery,
    or a date an admin set by hand). Read under the settings that produced `day`, so BEFORE a change."""
    if day is None:
        return None
    rule = _Rule(subscription)
    for back in range(1, _SEARCH_DAYS + 1):  # one cycle plus the move to a delivery day
        candidate = day - timedelta(days=back)
        if rule.is_anchor(candidate) and rule.advance(candidate) == day:
            return candidate
    return None


def delivery_after_change(
    subscription,
    today: date,
    last_delivery: Optional[date],
    *,
    order_created: bool,
    origin: Optional[date] = None,
) -> date:
    """The next delivery after the frequency, day or slot changed, under the NEW settings (§4.6, §9).

    Never sooner than the lead allows, and never on or before the later of the last delivery and `origin`
    (the `clock_origin` before the change). So a change never brings back a date that was skipped: a skip
    moved the clock on from that date (spec 2026-10-26 §12). When the last delivery's order is already
    created it is kept, so the next one waits a full new cycle after it: a change never gives two deliveries
    back to back."""
    rule = _Rule(subscription)
    soonest = earliest_delivery_date(today)
    counted_from = max((day for day in (last_delivery, origin) if day is not None), default=None)
    candidate = rule.first_on_or_after(soonest if counted_from is None else max(soonest, counted_from + _ONE_DAY))
    if order_created and last_delivery is not None:
        # The last delivery may sit on another day of the month (a day change), so it is not the schedule's own.
        return max(rule.advance(last_delivery, own_delivery=False), candidate)
    return candidate


def delivery_after_days_changed(subscription, old_next: date) -> date:
    """The next delivery after a slot write changed the delivery days (spec 2026-10-27 §6.4, §6.5), from the clock
    `old_next` dated under the old ones. Never earlier than `old_next`.

    A monthly date (with its day set) is counted from its own cycle's date, not from `old_next`: a date that moved
    under the old delivery days (Sunday the 31st to Monday the 1st) stays when a day added since brings the cycle's
    date back, so a search on from it never lands on the next month's date and drops a delivery. When the same write
    also took `old_next`'s weekday away, the delivery moves on to the next delivery day after it (§6.4), which may be
    more than a week after the month's date; it is still that month's delivery, and `advance` counts the next one from
    the month's date. Every other schedule takes its first delivery on or after `old_next`; a weekly or biweekly day
    that is no longer a delivery day is moved by the cascade itself (`next_delivery_weekday`)."""
    rule = _Rule(subscription)
    if rule._month_day is None:
        return rule.first_on_or_after(old_next)
    new = rule.first_on_or_after(_target_on_or_before(old_next, rule._month_day))
    if new >= old_next:
        return new
    if rule._on_a_delivery_day(old_next):
        return old_next
    weekday = next_delivery_weekday(old_next.weekday(), rule.delivery_days())
    return old_next + timedelta(days=(weekday - old_next.weekday()) % len(_WEEKDAYS))


def slot_offered_for(slot, frequency, weekday: Optional[int], *, delivery_days: Optional[frozenset] = None) -> bool:
    """Whether a recurring delivery may use `slot` (spec 2026-10-26 §8.2). The ONE rule behind the slot
    picker (`DeliveryService.recurring_slot_choice`) and the schedule guard below.

    A monthly delivery falls on a different delivery day each month, so its slot must be offered on every delivery
    day (`delivery_days`, read when not handed in; spec 2026-10-27 §6.4). Otherwise the slot must be offered on
    `weekday` (the chosen day of a weekly or biweekly delivery); with no weekday (daily, or the day not chosen yet)
    any active slot will do."""
    if slot is None or not slot.is_active:
        return False
    days = set(slot.available_days or ())
    if _frequency(frequency) == SubscriptionFrequency.MONTHLY:
        return days >= (delivery_weekdays() if delivery_days is None else delivery_days)
    return weekday is None or weekday in days


def slot_serves(subscription, slot, *, previous_days: frozenset) -> bool:
    """Whether `slot`, after a slot write, still serves this subscription (spec 2026-10-27 §6.5): never when it is
    switched off. A daily delivery needs every weekday it delivered on (`previous_days`); a weekly or biweekly or
    monthly one asks the slot picker's own rule (`slot_offered_for`)."""
    if slot is None or not slot.is_active:
        return False
    frequency = _frequency(subscription.frequency)
    if frequency == SubscriptionFrequency.DAILY:
        return set(slot.available_days or ()) >= previous_days
    if frequency == SubscriptionFrequency.MONTHLY:
        return slot_offered_for(slot, frequency, None)
    return slot_offered_for(slot, frequency, subscription.delivery_day_of_week)


DAY_OF_WEEK_REQUIRED = "day_of_week_required"
DAY_OF_MONTH_REQUIRED = "day_of_month_required"
DAY_NOT_DELIVERY_DAY = "day_not_delivery_day"
SLOT_NOT_OFFERED_ON_DAY = "slot_not_offered_on_day"
SLOT_NOT_EVERY_DAY = "slot_not_every_day"
# The stored slot does not cover the new schedule and none was chosen with it (spec 2026-10-27 §6.2): the customer
# must pick an offered slot or Any time; a slot is never kept or cleared silently.
SLOT_CHOICE_REQUIRED = "slot_choice_required"


def slot_covers(slot, frequency, weekday: Optional[int]) -> bool:
    """Whether `slot` serves this schedule: the ONE test behind "the stored slot is kept" and "choose a slot".
    It is `slot_offered_for`, the rule the slot picker uses, so what the picker offers the save accepts. A daily
    delivery with a slot lands on that slot's days (spec 2026-10-27 §6.4), so any active slot serves it."""
    return slot_offered_for(slot, frequency, weekday)


def schedule_refusal(subscription, *, slot_chosen: bool = True) -> Optional[str]:
    """Why this frequency, day and slot may not be saved, or None (spec 2026-10-26 §5 B9, §8.2): a weekly,
    biweekly or monthly delivery needs its day; a weekly or biweekly day must be a delivery day (spec 2026-10-27
    §6.4); and the slot must cover the schedule (`slot_covers`). Saving a slot that does not would silently
    turn deliveries into "Time to be confirmed".

    `slot_chosen` is whether the change carried a slot (an explicit one, or null for Any time). When the stored
    slot no longer covers the schedule and none was chosen, the answer is `SLOT_CHOICE_REQUIRED`; a chosen slot
    that does not cover it keeps its own code (`SLOT_NOT_OFFERED_ON_DAY`, `SLOT_NOT_EVERY_DAY`)."""
    frequency = _frequency(subscription.frequency)
    slot = subscription.delivery_time_slot
    weekday = None
    if frequency in (SubscriptionFrequency.WEEKLY, SubscriptionFrequency.BIWEEKLY):
        weekday = subscription.delivery_day_of_week
        if weekday is None:
            return DAY_OF_WEEK_REQUIRED
        if weekday not in delivery_weekdays():
            return DAY_NOT_DELIVERY_DAY
    elif frequency == SubscriptionFrequency.MONTHLY and subscription.delivery_day_of_month is None:
        return DAY_OF_MONTH_REQUIRED
    if slot is None or slot_covers(slot, frequency, weekday):
        return None
    if not slot_chosen:
        return SLOT_CHOICE_REQUIRED
    return SLOT_NOT_OFFERED_ON_DAY if weekday is not None else SLOT_NOT_EVERY_DAY


def validate_next_delivery(subscription, day: date, today: date) -> None:
    """Raise ValueError unless `day` may be the subscription's next delivery: one of its delivery days, no
    sooner than the lead allows. Admin "Set next delivery date" goes through this (§9)."""
    soonest = earliest_delivery_date(today)
    if day < soonest:
        raise ValueError(f"The next delivery cannot be before {soonest.isoformat()}")
    if first_delivery_on_or_after(subscription, day) != day:
        raise ValueError(f"{day.isoformat()} is not one of this subscription's delivery days")


def deliveries_per_month(frequency, slot=None, *, delivery_days: Optional[frozenset] = None) -> Decimal:
    """Deliveries in an average month (§4.2). Daily counts the weekdays it delivers on (`slot`'s, else the delivery
    days): 365 days x those weekdays / 7, over 12 months (spec 2026-10-27 §6.4). A caller over many rows hands in
    `delivery_days`, read once."""
    frequency = _frequency(frequency)
    if frequency != SubscriptionFrequency.DAILY:
        return _DELIVERIES_PER_MONTH[frequency]
    weekdays = _slot_weekdays(slot)
    if weekdays is None:
        weekdays = delivery_weekdays() if delivery_days is None else delivery_days
    return Decimal(365 * len(weekdays)) / Decimal(84)


def order_placed_on(day: date, today: date) -> date:
    """The day the order for the delivery on `day` is created: the lead before it (`due_through`), and
    never before today. The website's "We'll place this order on …" (spec 2026-10-26 §5 B3)."""
    return max(today, day - timedelta(days=SUBSCRIPTION_ORDER_LEAD_DAYS))


def next_deliveries(subscription, first: date, count: int) -> List[date]:
    """`count` scheduled deliveries from `first` (the clock) on, each one cycle after the one before."""
    rule = _Rule(subscription)
    days = []
    day = first
    while len(days) < count:
        days.append(day)
        day = rule.advance(day)
    return days


def slot_window(slot) -> Optional[Tuple[time, time]]:
    """The slot's window on any day it is offered, or None when there is no slot or it is inactive."""
    if slot is None or not slot.is_active:
        return None
    return parse_window_time(slot.start_time), parse_window_time(slot.end_time)


def window_for(slot, day: date) -> Optional[Tuple[time, time]]:
    """The slot's window on `day`, or None when there is no slot, it is inactive, or it is not offered
    that weekday; the order is then dated without a window. The slot's fees are never applied:
    delivery is always free and `create_order` keeps its own fee rule (§4.2)."""
    if slot is None or day.weekday() not in (slot.available_days or ()):
        return None
    return slot_window(slot)
