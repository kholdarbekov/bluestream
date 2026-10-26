"""`business_app/services/subscription_schedule.py`: the one schedule rule (spec 2026-10-23 §4.2, §9).

Every function is pure and takes its dates as arguments, so each case below is a frozen clock by
construction: "today" is the date handed in, never the wall clock. The calendar used throughout:

    2026-10-26 Mon   2026-10-27 Tue   2026-10-28 Wed   2026-10-29 Thu   2026-10-30 Fri

Subscriptions and slots are real (transient) model objects, so the module reads the same attribute
types production hands it: the frequency enum, `available_days` as a list, "HH:MM" strings.
"""

from datetime import date, time, timedelta
from decimal import Decimal
from itertools import islice

import pytest

from business_app.models.delivery import DeliveryTimeSlot
from business_app.models.subscription import Subscription
from business_app.services import subscription_schedule
from business_app.services.subscription_schedule import (
    advance,
    anchor_dates,
    deliveries_per_month,
    earliest_delivery_date,
    first_delivery_on_or_after,
    window_for,
)
from shared import business_config
from shared.enums import SubscriptionFrequency

pytestmark = pytest.mark.unit

MON, TUE, WED, THU, FRI = (date(2026, 10, 26) + timedelta(days=i) for i in range(5))
MONDAY, WEDNESDAY, THURSDAY, FRIDAY = 0, 2, 3, 4


def _slot(*, days=(0, 1, 2, 3, 4, 5, 6), active=True, start="10:00", end="12:00"):
    return DeliveryTimeSlot(name="Morning", start_time=start, end_time=end, is_active=active, available_days=list(days))


def _sub(frequency, *, day_of_week=None, day_of_month=None, slot=None):
    return Subscription(
        name="Water",
        frequency=frequency,
        delivery_day_of_week=day_of_week,
        delivery_day_of_month=day_of_month,
        delivery_time_slot=slot,
    )


def _take(iterator, n):
    return list(islice(iterator, n))


class TestAnchorDates:
    def test_weekly_on_thursday_from_a_monday(self):
        sub = _sub(SubscriptionFrequency.WEEKLY, day_of_week=THURSDAY)
        assert _take(anchor_dates(sub, MON), 3) == [THU, date(2026, 11, 5), date(2026, 11, 12)]

    def test_biweekly_anchors_on_its_weekday(self):
        """Parity is not an anchor property: `advance` keeps it (last + 14), §9."""
        sub = _sub(SubscriptionFrequency.BIWEEKLY, day_of_week=THURSDAY)
        assert _take(anchor_dates(sub, MON), 2) == [THU, date(2026, 11, 5)]

    def test_monthly_on_the_31st_clamps_to_each_months_last_day(self):
        sub = _sub(SubscriptionFrequency.MONTHLY, day_of_month=31)
        assert _take(anchor_dates(sub, date(2027, 1, 15)), 4) == [
            date(2027, 1, 31),
            date(2027, 2, 28),
            date(2027, 3, 31),
            date(2027, 4, 30),
        ]

    def test_daily_uses_the_slots_available_days(self):
        sub = _sub(SubscriptionFrequency.DAILY, slot=_slot(days=(MONDAY, WEDNESDAY, FRIDAY)))
        assert _take(anchor_dates(sub, TUE), 3) == [WED, FRI, date(2026, 11, 2)]

    def test_daily_without_a_slot_is_every_day(self):
        assert _take(anchor_dates(_sub(SubscriptionFrequency.DAILY), TUE), 3) == [TUE, WED, THU]

    @pytest.mark.parametrize(
        "slot",
        [_slot(days=(MONDAY,), active=False), _slot(days=())],
        ids=["inactive-slot", "slot-without-days"],
    )
    def test_daily_with_a_slot_that_offers_no_day_is_every_day(self, slot):
        """An inactive or dayless slot means "dated, time to be confirmed" (§6.6), never "no delivery"."""
        assert _take(anchor_dates(_sub(SubscriptionFrequency.DAILY, slot=slot), TUE), 2) == [TUE, WED]

    def test_a_day_that_is_not_set_anchors_on_every_day(self):
        assert _take(anchor_dates(_sub(SubscriptionFrequency.WEEKLY), TUE), 2) == [TUE, WED]
        assert _take(anchor_dates(_sub(SubscriptionFrequency.MONTHLY), TUE), 2) == [TUE, WED]

    @pytest.mark.parametrize(
        "sub",
        [
            _sub(SubscriptionFrequency.WEEKLY, day_of_week=7),
            _sub(SubscriptionFrequency.BIWEEKLY, day_of_week=-1),
            _sub(SubscriptionFrequency.MONTHLY, day_of_month=0),
            _sub(SubscriptionFrequency.MONTHLY, day_of_month=32),
        ],
        ids=["weekday-7", "weekday-minus-1", "month-day-0", "month-day-32"],
    )
    def test_an_impossible_day_is_refused_instead_of_searched_forever(self, sub):
        with pytest.raises(ValueError):
            next(anchor_dates(sub, MON))


class TestFirstDeliveryOnOrAfter:
    def test_weekly_thursday(self):
        sub = _sub(SubscriptionFrequency.WEEKLY, day_of_week=THURSDAY)
        assert first_delivery_on_or_after(sub, MON) == THU
        assert first_delivery_on_or_after(sub, THU) == THU  # on or after: the day itself counts
        assert first_delivery_on_or_after(sub, FRI) == date(2026, 11, 5)

    def test_monthly_31st_in_a_30_day_month(self):
        sub = _sub(SubscriptionFrequency.MONTHLY, day_of_month=31)
        assert first_delivery_on_or_after(sub, date(2026, 11, 2)) == date(2026, 11, 30)

    def test_daily_skips_days_the_slot_is_not_available(self):
        sub = _sub(SubscriptionFrequency.DAILY, slot=_slot(days=(MONDAY, WEDNESDAY, FRIDAY)))
        assert first_delivery_on_or_after(sub, TUE) == WED


class TestAdvance:
    def test_weekly_thursday_to_the_following_thursday(self):
        sub = _sub(SubscriptionFrequency.WEEKLY, day_of_week=THURSDAY)
        assert advance(sub, THU) == date(2026, 11, 5)

    def test_monthly_31st_lands_on_feb_28_and_apr_30_and_returns_to_the_31st(self):
        sub = _sub(SubscriptionFrequency.MONTHLY, day_of_month=31)
        seen, day = [], date(2027, 1, 31)
        for _ in range(4):
            day = advance(sub, day)
            seen.append(day)
        assert seen == [date(2027, 2, 28), date(2027, 3, 31), date(2027, 4, 30), date(2027, 5, 31)]

    def test_monthly_31st_lands_on_feb_29_in_a_leap_year(self):
        sub = _sub(SubscriptionFrequency.MONTHLY, day_of_month=31)
        assert advance(sub, date(2028, 1, 31)) == date(2028, 2, 29)

    def test_biweekly_is_last_plus_14_days_and_keeps_its_parity(self):
        sub = _sub(SubscriptionFrequency.BIWEEKLY, day_of_week=THURSDAY)
        first = advance(sub, THU)
        assert (first, advance(sub, first)) == (date(2026, 11, 12), date(2026, 11, 26))

    def test_a_changed_day_counts_one_full_cycle_from_the_last_delivery(self):
        """§9: never two deliveries back to back. Last delivery Thursday, new day Monday: the next is
        the first Monday on or after Thursday + 7, not the Monday right after Thursday."""
        sub = _sub(SubscriptionFrequency.WEEKLY, day_of_week=MONDAY)
        assert advance(sub, THU) == date(2026, 11, 9)

    def test_daily_with_slot_days(self):
        sub = _sub(SubscriptionFrequency.DAILY, slot=_slot(days=(MONDAY, WEDNESDAY, FRIDAY)))
        assert advance(sub, MON) == WED
        assert advance(sub, FRI) == date(2026, 11, 2)

    def test_daily_without_a_slot_is_the_next_day(self):
        assert advance(_sub(SubscriptionFrequency.DAILY), MON) == TUE

    def test_no_day_set_keeps_the_cadence_of_the_last_delivery(self):
        assert advance(_sub(SubscriptionFrequency.WEEKLY), TUE) == date(2026, 11, 3)
        assert advance(_sub(SubscriptionFrequency.BIWEEKLY), TUE) == date(2026, 11, 10)
        assert advance(_sub(SubscriptionFrequency.MONTHLY), date(2027, 1, 15)) == date(2027, 2, 15)


class TestEarliestDeliveryDate:
    def test_the_lead_is_one_day(self):
        assert business_config.SUBSCRIPTION_ORDER_LEAD_DAYS == 1
        assert isinstance(business_config.SUBSCRIPTION_ORDER_LEAD_DAYS, int)
        assert earliest_delivery_date(MON) == TUE

    def test_the_lead_is_read_from_business_config(self, monkeypatch):
        monkeypatch.setattr(subscription_schedule, "SUBSCRIPTION_ORDER_LEAD_DAYS", 2)
        assert earliest_delivery_date(MON) == WED


class TestDeliveriesPerMonth:
    @pytest.mark.parametrize(
        "frequency, expected",
        [
            (SubscriptionFrequency.DAILY, Decimal(365) / Decimal(12)),
            (SubscriptionFrequency.WEEKLY, Decimal(52) / Decimal(12)),
            (SubscriptionFrequency.BIWEEKLY, Decimal(26) / Decimal(12)),
            (SubscriptionFrequency.MONTHLY, Decimal(1)),
        ],
    )
    def test_calendar_accurate(self, frequency, expected):
        assert deliveries_per_month(frequency) == expected
        assert isinstance(deliveries_per_month(frequency), Decimal)

    def test_accepts_the_wire_value(self):
        assert deliveries_per_month("weekly") == Decimal(52) / Decimal(12)


class TestWindowFor:
    def test_the_slots_times_on_a_day_it_is_available(self):
        assert window_for(_slot(days=(THURSDAY,)), THU) == (time(10, 0), time(12, 0))

    def test_no_window_on_a_day_the_slot_is_not_available(self):
        assert window_for(_slot(days=(MONDAY,)), THU) is None

    def test_no_window_for_an_inactive_slot(self):
        assert window_for(_slot(active=False), THU) is None

    def test_no_window_without_a_slot(self):
        assert window_for(None, THU) is None


class TestFirstDelivery:
    """A new subscription's first delivery and the first after a resume (§4.3, §4.6)."""

    def test_never_sooner_than_the_lead_allows(self):
        assert subscription_schedule.first_delivery(_sub(SubscriptionFrequency.WEEKLY), MON) == TUE

    def test_the_first_anchor_after_the_lead(self):
        sub = _sub(SubscriptionFrequency.WEEKLY, day_of_week=THURSDAY)
        assert subscription_schedule.first_delivery(sub, MON) == THU
        assert subscription_schedule.first_delivery(sub, THU) == date(2026, 11, 5)  # Thursday itself is too soon

    def test_a_later_start_date_wins(self):
        sub = _sub(SubscriptionFrequency.WEEKLY, day_of_week=THURSDAY)
        assert subscription_schedule.first_delivery(sub, MON, start=date(2026, 11, 3)) == date(2026, 11, 5)

    def test_an_earlier_start_date_does_not_beat_the_lead(self):
        sub = _sub(SubscriptionFrequency.WEEKLY)
        assert subscription_schedule.first_delivery(sub, MON, start=date(2026, 10, 1)) == TUE


class TestDueThrough:
    def test_an_order_is_created_the_lead_before_its_delivery(self):
        assert subscription_schedule.due_through(WED) == THU

    def test_it_follows_the_configured_lead(self, monkeypatch):
        monkeypatch.setattr(subscription_schedule, "SUBSCRIPTION_ORDER_LEAD_DAYS", 2)
        assert subscription_schedule.due_through(MON) == WED


class TestCatchUp:
    """§2.8: a date that has already passed is never back-dated; every passed date is skipped."""

    def test_nothing_to_skip_when_the_date_is_today_or_later(self):
        sub = _sub(SubscriptionFrequency.WEEKLY, day_of_week=THURSDAY)
        assert subscription_schedule.catch_up(sub, THU, THU) == (THU, [])

    def test_one_passed_delivery_moves_to_the_next_on_its_cadence(self):
        sub = _sub(SubscriptionFrequency.WEEKLY, day_of_week=THURSDAY)
        assert subscription_schedule.catch_up(sub, THU, FRI) == (date(2026, 11, 5), [THU])

    def test_several_passed_deliveries_are_all_skipped_at_once(self):
        sub = _sub(SubscriptionFrequency.DAILY, slot=_slot(days=(MONDAY, WEDNESDAY, FRIDAY)))
        assert subscription_schedule.catch_up(sub, MON, date(2026, 10, 31)) == (date(2026, 11, 2), [MON, WED, FRI])

    def test_biweekly_keeps_its_parity(self):
        sub = _sub(SubscriptionFrequency.BIWEEKLY, day_of_week=THURSDAY)
        assert subscription_schedule.catch_up(sub, THU, date(2026, 11, 6)) == (date(2026, 11, 12), [THU])


class TestDeliveryAfterChange:
    """The next delivery after a frequency, day or slot change, under the NEW settings (§4.6, §9)."""

    def test_no_delivery_yet_is_the_first_anchor_after_the_lead(self):
        sub = _sub(SubscriptionFrequency.WEEKLY, day_of_week=FRIDAY)
        assert subscription_schedule.delivery_after_change(sub, MON, None, order_created=False) == FRI

    def test_never_the_day_of_the_last_delivery(self):
        """Last delivery today-ish: the next is at least the day after it (and after the lead)."""
        sub = _sub(SubscriptionFrequency.DAILY)
        assert subscription_schedule.delivery_after_change(sub, MON, WED, order_created=False) == THU

    def test_with_the_order_created_it_counts_one_full_new_cycle_from_it(self):
        """§9: weekly Thursday, Thursday's order exists, the day moves to Friday. Friday the 30th would
        be the day after Thursday's delivery, so the next is Friday the 6th."""
        sub = _sub(SubscriptionFrequency.WEEKLY, day_of_week=FRIDAY)
        assert subscription_schedule.delivery_after_change(sub, TUE, THU, order_created=True) == date(2026, 11, 6)

    def test_without_the_order_created_the_new_day_can_come_sooner(self):
        sub = _sub(SubscriptionFrequency.WEEKLY, day_of_week=FRIDAY)
        assert subscription_schedule.delivery_after_change(sub, TUE, date(2026, 10, 22), order_created=False) == FRI

    def test_the_lead_still_applies_after_an_old_delivery(self):
        sub = _sub(SubscriptionFrequency.MONTHLY)
        assert subscription_schedule.delivery_after_change(sub, MON, date(2026, 9, 1), order_created=True) == TUE


class TestValidateNextDelivery:
    """Admin "Set next delivery date" is checked by the rule, not trusted (§9)."""

    def test_an_anchor_after_the_lead_is_accepted(self):
        sub = _sub(SubscriptionFrequency.WEEKLY, day_of_week=THURSDAY)
        subscription_schedule.validate_next_delivery(sub, date(2026, 11, 12), MON)

    def test_a_day_that_is_not_a_delivery_day_is_refused(self):
        sub = _sub(SubscriptionFrequency.WEEKLY, day_of_week=THURSDAY)
        with pytest.raises(ValueError, match="2026-11-10"):
            subscription_schedule.validate_next_delivery(sub, date(2026, 11, 10), MON)

    def test_a_day_sooner_than_the_lead_is_refused(self):
        with pytest.raises(ValueError, match="2026-10-27"):
            subscription_schedule.validate_next_delivery(_sub(SubscriptionFrequency.DAILY), MON, MON)
