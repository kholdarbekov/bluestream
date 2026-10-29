"""`business_app/services/subscription_schedule.py`: the one schedule rule (spec 2026-10-23 §4.2, §9).

Every function is pure and takes its dates as arguments, so each case below is a frozen clock by
construction: "today" is the date handed in, never the wall clock. The calendar used throughout:

    2026-10-26 Mon   2026-10-27 Tue   2026-10-28 Wed   2026-10-29 Thu   2026-10-30 Fri

Subscriptions and slots are real (transient) model objects, so the module reads the same attribute
types production hands it: the frequency enum, `available_days` as a list, "HH:MM" strings. The delivery
days (spec 2026-10-27 §6.4) are the module's one database read: the weekdays the ACTIVE slot rows offer,
every day when there are none. So every test runs on the test database, and the tests that predate delivery
days, which define no slot row, see every day as a delivery day.
"""

from datetime import date, time, timedelta
from decimal import Decimal
from itertools import combinations, islice

import pytest

from business_app.models.delivery import DeliveryTimeSlot
from business_app.models.subscription import Subscription
from business_app.services import subscription_schedule
from business_app.services.subscription_schedule import (
    advance,
    anchor_dates,
    clock_origin,
    deliveries_per_month,
    delivery_after_days_changed,
    delivery_weekdays,
    earliest_delivery_date,
    first_delivery_on_or_after,
    schedule_refusal,
    slot_offered_for,
    validate_next_delivery,
    weekdays_offered,
    window_for,
)
from shared import business_config
from shared.enums import SubscriptionFrequency

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("db")]

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


def _offer(db, *day_lists, active=True):
    """Real slot rows, committed: what the delivery days are read from."""
    slots = [
        DeliveryTimeSlot(name=f"Slot {n}", start_time="10:00", end_time="12:00", is_active=active,
                         available_days=list(days))
        for n, days in enumerate(day_lists)
    ]
    db.session.add_all(slots)
    db.session.commit()
    return slots


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
        """An inactive or dayless slot delivers on the delivery days (here, with no slot row, every day), its time
        to be confirmed; never "no delivery"."""
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

    def test_a_monthly_day_moved_earlier_waits_a_full_cycle_after_the_created_delivery(self):
        """§9 with a monthly day: the 10th becomes the 8th while the 10 March order exists. A last delivery on
        another day of the month is not the schedule's own, so it counts one full cycle from itself: the first 8th
        on or after 10 April is 8 May. 8 April would be 29 days after it."""
        sub = _sub(SubscriptionFrequency.MONTHLY, day_of_month=8)
        assert subscription_schedule.delivery_after_change(
            sub, date(2027, 3, 9), date(2027, 3, 10), order_created=True
        ) == date(2027, 5, 8)

    def test_a_skipped_date_never_comes_back(self):
        """Spec 2026-10-26 §12: weekly Thursday, the 29th skipped (the clock moved to 5 November). The slot
        changes, the day stays: the next is 5 November, not the skipped 29th."""
        sub = _sub(SubscriptionFrequency.WEEKLY, day_of_week=THURSDAY)
        origin = subscription_schedule.clock_origin(sub, date(2026, 11, 5))
        assert origin == THU
        assert subscription_schedule.delivery_after_change(sub, MON, None, order_created=False, origin=origin) == (
            date(2026, 11, 5)
        )


class TestClockOrigin:
    """The scheduled date the clock advanced from: `advance(origin) == day` (spec 2026-10-26 §12)."""

    def test_weekly(self):
        sub = _sub(SubscriptionFrequency.WEEKLY, day_of_week=THURSDAY)
        assert subscription_schedule.clock_origin(sub, date(2026, 11, 5)) == THU

    def test_biweekly_is_two_weeks_back_not_one(self):
        sub = _sub(SubscriptionFrequency.BIWEEKLY, day_of_week=THURSDAY)
        assert subscription_schedule.clock_origin(sub, date(2026, 11, 12)) == THU

    def test_monthly_on_the_31st_after_the_february_clamp(self):
        sub = _sub(SubscriptionFrequency.MONTHLY, day_of_month=31)
        assert subscription_schedule.clock_origin(sub, date(2027, 2, 28)) == date(2027, 1, 31)

    def test_daily_over_a_weekend_the_slot_skips(self):
        sub = _sub(SubscriptionFrequency.DAILY, slot=_slot(days=(0, 1, 2, 3, 4)))
        assert subscription_schedule.clock_origin(sub, date(2026, 11, 2)) == FRI

    def test_none_has_no_origin(self):
        sub = _sub(SubscriptionFrequency.WEEKLY, day_of_week=THURSDAY)
        assert subscription_schedule.clock_origin(sub, None) is None


class TestTheSlotRule:
    """One rule for the slot picker and the save guard (spec 2026-10-26 §8.2)."""

    @pytest.mark.parametrize(
        "frequency, weekday, days, offered",
        [
            (SubscriptionFrequency.WEEKLY, THURSDAY, (0, 1, 2, 3, 4), True),
            (SubscriptionFrequency.WEEKLY, 5, (0, 1, 2, 3, 4), False),
            (SubscriptionFrequency.BIWEEKLY, None, (0,), True),
            (SubscriptionFrequency.MONTHLY, None, (0, 1, 2, 3, 4), False),
            (SubscriptionFrequency.MONTHLY, None, (0, 1, 2, 3, 4, 5, 6), True),
            (SubscriptionFrequency.DAILY, None, (5, 6), True),
        ],
    )
    def test_slot_offered_for(self, frequency, weekday, days, offered):
        assert subscription_schedule.slot_offered_for(_slot(days=days), frequency, weekday) is offered

    def test_an_inactive_slot_is_offered_for_nothing(self):
        assert subscription_schedule.slot_offered_for(_slot(active=False), SubscriptionFrequency.DAILY, None) is False

    @pytest.mark.parametrize(
        "sub, refusal",
        [
            (_sub(SubscriptionFrequency.WEEKLY), "day_of_week_required"),
            (_sub(SubscriptionFrequency.BIWEEKLY), "day_of_week_required"),
            (_sub(SubscriptionFrequency.MONTHLY), "day_of_month_required"),
            (_sub(SubscriptionFrequency.WEEKLY, day_of_week=5, slot=_slot(days=(0, 1, 2, 3, 4))),
             "slot_not_offered_on_day"),
            (_sub(SubscriptionFrequency.MONTHLY, day_of_month=15, slot=_slot(days=(0, 1, 2, 3, 4))),
             "slot_not_every_day"),
            (_sub(SubscriptionFrequency.WEEKLY, day_of_week=THURSDAY, slot=_slot()), None),
            (_sub(SubscriptionFrequency.MONTHLY, day_of_month=15), None),
            # A daily delivery with a slot lands on that slot's days (spec 2026-10-27 §6.4): any active slot serves it.
            (_sub(SubscriptionFrequency.DAILY, slot=_slot(days=(5, 6))), None),
            (_sub(SubscriptionFrequency.DAILY, slot=_slot()), None),
            (_sub(SubscriptionFrequency.DAILY), None),
        ],
    )
    def test_schedule_refusal(self, sub, refusal):
        assert subscription_schedule.schedule_refusal(sub) == refusal

    def test_a_slot_that_does_not_cover_the_schedule_asks_for_a_choice_unless_one_was_chosen(self):
        sub = _sub(SubscriptionFrequency.WEEKLY, day_of_week=5, slot=_slot(days=(0, 1, 2, 3, 4)))

        assert subscription_schedule.schedule_refusal(sub, slot_chosen=False) == "slot_choice_required"
        assert subscription_schedule.schedule_refusal(sub, slot_chosen=True) == "slot_not_offered_on_day"
        covered = _sub(SubscriptionFrequency.WEEKLY, day_of_week=THURSDAY, slot=_slot())
        assert subscription_schedule.schedule_refusal(covered, slot_chosen=False) is None
        assert subscription_schedule.schedule_refusal(_sub(SubscriptionFrequency.WEEKLY, day_of_week=5), slot_chosen=False) is None


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


# --------------------------------------------------------------------------- delivery days (spec 2026-10-27 §6.4)

DAILY, WEEKLY, BIWEEKLY, MONTHLY = (
    SubscriptionFrequency.DAILY,
    SubscriptionFrequency.WEEKLY,
    SubscriptionFrequency.BIWEEKLY,
    SubscriptionFrequency.MONTHLY,
)
MON_TO_SAT = (0, 1, 2, 3, 4, 5)
SUNDAY = 6


class TestDeliveryWeekdays:
    """A delivery day is a weekday at least one active slot offers; with none, every day is (§17 Q7)."""

    def test_every_day_when_no_slot_is_defined(self):
        assert delivery_weekdays() == frozenset(range(7))

    def test_every_day_when_every_slot_is_switched_off(self, db):
        _offer(db, [0], active=False)
        assert delivery_weekdays() == frozenset(range(7))

    def test_the_union_of_the_active_slots_days(self, db):
        _offer(db, [0, 1], [5])
        _offer(db, [6], active=False)
        assert delivery_weekdays() == frozenset({0, 1, 5})

    def test_weekdays_offered_is_the_pure_rule(self):
        assert weekdays_offered([[0, 1], [9], []]) == frozenset({0, 1})
        assert weekdays_offered([]) == frozenset(range(7))


class TestDailyOnDeliveryDays:
    """Daily delivers on delivery days only, slot or not (§6.4). Monday to Saturday offered."""

    def test_daily_without_a_slot_skips_a_day_no_slot_offers(self, db):
        _offer(db, MON_TO_SAT)
        assert _take(anchor_dates(_sub(DAILY), date(2026, 10, 31)), 3) == [
            date(2026, 10, 31),
            date(2026, 11, 2),
            date(2026, 11, 3),
        ]
        assert advance(_sub(DAILY), date(2026, 10, 31)) == date(2026, 11, 2)

    def test_daily_with_an_inactive_slot_uses_the_delivery_days(self, db):
        _offer(db, MON_TO_SAT)
        sub = _sub(DAILY, slot=_slot(days=(SUNDAY,), active=False))
        assert first_delivery_on_or_after(sub, date(2026, 11, 1)) == date(2026, 11, 2)

    def test_daily_with_an_active_slot_keeps_its_own_days(self, db):
        _offer(db, MON_TO_SAT)
        sub = _sub(DAILY, slot=_slot(days=(MONDAY, WEDNESDAY, FRIDAY)))
        assert _take(anchor_dates(sub, TUE), 3) == [WED, FRI, date(2026, 11, 2)]


class TestMonthlyRollsToTheNextDeliveryDay:
    """A monthly date on a non-delivery day moves to the next delivery day (§6.4). Monday to Saturday offered;
    2027-01-31 and 2027-02-28 are Sundays."""

    def test_a_target_on_a_sunday_moves_to_monday(self, db):
        _offer(db, MON_TO_SAT)
        assert _take(anchor_dates(_sub(MONTHLY, day_of_month=31), date(2027, 1, 15)), 3) == [
            date(2027, 2, 1),
            date(2027, 3, 1),
            date(2027, 3, 31),
        ]

    def test_advance_counts_from_the_target_not_the_rolled_date(self, db):
        """January's delivery, rolled to 1 February, is still January's: the next is February's (rolled to 1
        March), never a skipped February."""
        _offer(db, MON_TO_SAT)
        sub = _sub(MONTHLY, day_of_month=31)
        assert advance(sub, date(2027, 2, 1)) == date(2027, 3, 1)
        assert advance(sub, date(2027, 3, 1)) == date(2027, 3, 31)

    def test_clock_origin_of_a_rolled_date(self, db):
        _offer(db, MON_TO_SAT)
        assert clock_origin(_sub(MONTHLY, day_of_month=31), date(2027, 3, 1)) == date(2027, 2, 1)

    def test_the_rolled_date_is_a_delivery_day_and_the_sunday_target_is_not(self, db):
        _offer(db, MON_TO_SAT)
        sub = _sub(MONTHLY, day_of_month=31)
        validate_next_delivery(sub, date(2027, 3, 1), date(2027, 1, 15))
        with pytest.raises(ValueError, match="2027-02-28"):
            validate_next_delivery(sub, date(2027, 2, 28), date(2027, 1, 15))

    def test_a_target_on_a_delivery_day_stays(self, db):
        _offer(db, MON_TO_SAT)
        sub = _sub(MONTHLY, day_of_month=15)
        assert _take(anchor_dates(sub, date(2027, 1, 1)), 2) == [date(2027, 1, 15), date(2027, 2, 15)]


class TestPerMonthEstimate:
    """The per-month estimate counts the real delivery days (§6.4): 365 days x k weekdays out of 7, over 12."""

    def test_daily_counts_the_delivery_days(self, db):
        _offer(db, MON_TO_SAT)
        assert deliveries_per_month(DAILY) == Decimal(365 * 6) / Decimal(84)
        assert deliveries_per_month(DAILY, slot=_slot(days=(0, 1, 2, 3, 4))) == Decimal(365 * 5) / Decimal(84)
        assert deliveries_per_month(WEEKLY) == Decimal(52) / Decimal(12)

    def test_an_inactive_slot_counts_the_delivery_days(self, db):
        _offer(db, MON_TO_SAT)
        assert deliveries_per_month(DAILY, slot=_slot(days=(0,), active=False)) == Decimal(365 * 6) / Decimal(84)

    def test_the_delivery_days_a_caller_read_once_are_used_as_given(self, db, count_queries):
        """A caller over many rows (the admin list, MRR) reads the delivery days once and hands them down."""
        with count_queries() as counted:
            assert deliveries_per_month(DAILY, delivery_days=frozenset({0, 1, 2})) == Decimal(365 * 3) / Decimal(84)
        assert counted.count == 0


def _all_day_sets():
    return [days for size in range(1, 8) for days in combinations(range(7), size)]


class TestNoScheduleCanLoop:
    """§6.5: the schedule functions handle any set of delivery days, with no search that can fail to end."""

    def test_every_delivery_day_set_finds_a_date_for_every_frequency(self, db):
        [slot] = _offer(db, [0])
        starts = (date(2027, 1, 25), date(2027, 2, 27), date(2028, 2, 28))
        for days in _all_day_sets():
            slot.available_days = list(days)
            db.session.commit()
            subs = (
                _sub(DAILY),
                _sub(WEEKLY, day_of_week=days[0]),
                _sub(WEEKLY),
                _sub(BIWEEKLY, day_of_week=days[-1]),
                _sub(MONTHLY, day_of_month=31),
                _sub(MONTHLY),
            )
            for sub in subs:
                for start in starts:
                    found = first_delivery_on_or_after(sub, start)
                    case = (days, sub.frequency, sub.delivery_day_of_week, sub.delivery_day_of_month, start)
                    assert start <= found <= start + timedelta(days=37), case
                    assert found.weekday() in days, case
                    following = advance(sub, found)
                    assert following > found and following.weekday() in days, case

    def test_the_search_is_bounded(self, monkeypatch):
        """A rule that could never hold (no delivery day at all, which `weekdays_offered` never returns) raises
        instead of searching forever."""
        monkeypatch.setattr(subscription_schedule, "delivery_weekdays", lambda: frozenset())
        with pytest.raises(ValueError):
            first_delivery_on_or_after(_sub(DAILY), MON)

    def test_one_delivery_day_read_per_call(self, db, count_queries):
        with count_queries() as counted:
            clock_origin(_sub(DAILY), THU)
        assert counted.count == 1
        with count_queries() as counted:
            clock_origin(_sub(WEEKLY, day_of_week=THURSDAY), THU)
        assert counted.count == 0
        with count_queries() as counted:
            subscription_schedule.catch_up(_sub(MONTHLY, day_of_month=31), date(2027, 1, 31), date(2027, 6, 1))
        assert counted.count == 1
        with count_queries() as counted:
            subscription_schedule.next_deliveries(_sub(DAILY), MON, 5)
        assert counted.count == 1


class TestAnAddedDeliveryDayNeverSkipsAMonth:
    """A monthly date that moved under the delivery days of its time is still its month's after a delivery day is
    added (§6.4, §6.5): the next delivery is the next month's, never the month after. The slot rows change between
    the moved delivery and the next one, as an admin's slot write does. Monday to Saturday first; 2027-01-31 and
    2027-02-28 are Sundays, 2027-02-01 and 2027-03-01 Mondays."""

    def test_february_moved_to_1_march_is_followed_by_march_after_a_sunday_slot(self, db):
        _offer(db, MON_TO_SAT)
        sub = _sub(MONTHLY, day_of_month=31)
        february = first_delivery_on_or_after(sub, date(2027, 2, 28))
        assert february == date(2027, 3, 1)
        _offer(db, [SUNDAY])
        assert advance(sub, february) == date(2027, 3, 31)

    def test_january_moved_to_1_february_is_followed_by_february_after_a_sunday_slot(self, db):
        """The clock on the moved 1 February: the daily run, the catch-up and the upcoming dates all keep 28
        February."""
        _offer(db, MON_TO_SAT)
        sub = _sub(MONTHLY, day_of_month=31)
        january = first_delivery_on_or_after(sub, date(2027, 1, 31))
        assert january == date(2027, 2, 1)
        _offer(db, [SUNDAY])
        assert advance(sub, january) == date(2027, 2, 28)
        assert subscription_schedule.next_deliveries(sub, january, 4) == [
            date(2027, 2, 1),
            date(2027, 2, 28),
            date(2027, 3, 31),
            date(2027, 4, 30),
        ]
        assert subscription_schedule.catch_up(sub, january, date(2027, 3, 15)) == (
            date(2027, 3, 31),
            [date(2027, 2, 1), date(2027, 2, 28)],
        )

    def test_a_slot_switched_back_on_or_widened_counts_the_same(self, db):
        """The 1st, Tuesday to Saturday: Monday 1 February moves to Tuesday the 2nd. Monday comes back either way an
        admin can bring it: the next is Monday 1 March, not 1 April."""
        weekdays, monday = _offer(db, (1, 2, 3, 4, 5), [MONDAY])
        monday.is_active = False
        db.session.commit()
        sub = _sub(MONTHLY, day_of_month=1)
        moved = first_delivery_on_or_after(sub, date(2027, 2, 1))
        assert moved == date(2027, 2, 2)
        monday.is_active = True
        db.session.commit()
        assert advance(sub, moved) == date(2027, 3, 1)
        monday.is_active = False
        weekdays.available_days = [0, 1, 2, 3, 4, 5]
        db.session.commit()
        assert advance(sub, moved) == date(2027, 3, 1)

    def test_every_added_delivery_day_keeps_every_monthly_cycle(self, db):
        """Every set of delivery days, and every day that can be added to it: a monthly delivery dated under the old
        days is followed by the schedule's very next delivery day under the new ones, a month on and no more."""
        [slot] = _offer(db, [0])
        starts = (date(2027, 1, 1), date(2027, 2, 1), date(2028, 2, 1))
        subs = [_sub(MONTHLY, day_of_month=day) for day in (1, 29, 31)]
        for days in _all_day_sets():
            slot.available_days = list(days)
            db.session.commit()
            dated = [(sub, first_delivery_on_or_after(sub, start)) for sub in subs for start in starts]
            for added in sorted(set(range(7)) - set(days)):
                slot.available_days = sorted({*days, added})
                db.session.commit()
                for sub, day in dated:
                    case = (days, added, sub.delivery_day_of_month, day)
                    following = advance(sub, day)
                    assert following == first_delivery_on_or_after(sub, day + timedelta(days=1)), case
                    assert following - day <= timedelta(days=31 + 6), case


class TestScheduleRefusalDeliveryDays:
    def test_a_weekly_day_that_is_not_a_delivery_day(self, db):
        _offer(db, MON_TO_SAT)
        assert schedule_refusal(_sub(WEEKLY, day_of_week=SUNDAY)) == "day_not_delivery_day"
        assert schedule_refusal(_sub(BIWEEKLY, day_of_week=SUNDAY)) == "day_not_delivery_day"
        assert schedule_refusal(_sub(WEEKLY, day_of_week=5)) is None
        # The day is asked for first; the slot is judged only on a delivery day.
        assert schedule_refusal(_sub(WEEKLY)) == "day_of_week_required"
        assert schedule_refusal(_sub(WEEKLY, day_of_week=SUNDAY, slot=_slot(days=(0,)))) == "day_not_delivery_day"

    def test_monthly_needs_its_slot_on_every_delivery_day_not_every_weekday(self, db):
        _offer(db, MON_TO_SAT)
        assert schedule_refusal(_sub(MONTHLY, day_of_month=15, slot=_slot(days=MON_TO_SAT))) is None
        assert schedule_refusal(_sub(MONTHLY, day_of_month=15, slot=_slot(days=(0, 1, 2, 3, 4)))) == (
            "slot_not_every_day"
        )
        assert slot_offered_for(_slot(days=MON_TO_SAT), MONTHLY, None) is True
        assert slot_offered_for(_slot(days=(0, 1, 2, 3, 4)), MONTHLY, None, delivery_days=frozenset(range(5)))


class TestDeliveryAfterDaysChanged:
    """The next delivery after a slot write changed the delivery days (spec 2026-10-27 §6.4, §6.5): never earlier,
    and a monthly date stays in its own cycle. The slot rows change between the dated delivery and the call, as an
    admin's slot write does."""

    def test_a_sunday_added_keeps_januarys_moved_31st(self, db):
        _offer(db, MON_TO_SAT)
        sub = _sub(MONTHLY, day_of_month=31)
        old_next = first_delivery_on_or_after(sub, date(2027, 1, 31))
        assert old_next == date(2027, 2, 1)
        _offer(db, [SUNDAY])
        assert delivery_after_days_changed(sub, old_next) == date(2027, 2, 1)

    def test_a_monday_added_keeps_februarys_moved_1st(self, db):
        _offer(db, (1, 2, 3, 4, 5))
        sub = _sub(MONTHLY, day_of_month=1)
        old_next = first_delivery_on_or_after(sub, date(2027, 2, 1))
        assert old_next == date(2027, 2, 2)
        _offer(db, [MONDAY])
        assert delivery_after_days_changed(sub, old_next) == date(2027, 2, 2)

    def test_a_lost_wednesday_moves_the_31st_to_the_next_delivery_day(self, db):
        [slot] = _offer(db, range(7))
        sub = _sub(MONTHLY, day_of_month=31)
        old_next = first_delivery_on_or_after(sub, date(2027, 3, 31))
        assert old_next == date(2027, 3, 31)
        slot.available_days = [0, 1, 3, 4, 5, 6]
        db.session.commit()
        assert delivery_after_days_changed(sub, old_next) == date(2027, 4, 1)

    def test_a_weekly_day_is_left_to_the_day_move(self, db):
        """A weekly Sunday that is no longer a delivery day keeps its date here: the cascade moves the day itself."""
        [slot] = _offer(db, range(7))
        sub = _sub(WEEKLY, day_of_week=SUNDAY)
        slot.available_days = list(MON_TO_SAT)
        db.session.commit()
        assert delivery_after_days_changed(sub, date(2027, 1, 31)) == date(2027, 1, 31)

    def test_a_cycle_whose_date_comes_back_and_whose_moved_day_goes_moves_to_the_next_delivery_day(self, db):
        """One write adds the 31st's Sunday and removes the Monday it had moved to: the delivery moves on to the next
        delivery day after it (§6.4), never back before the date the customer was told."""
        [slot] = _offer(db, MON_TO_SAT)
        sub = _sub(MONTHLY, day_of_month=31)
        old_next = first_delivery_on_or_after(sub, date(2027, 1, 31))
        assert old_next == date(2027, 2, 1)
        slot.available_days = [1, 2, 3, 4, 5, 6]
        db.session.commit()
        moved = delivery_after_days_changed(sub, old_next)
        assert moved == date(2027, 2, 2)
        assert advance(sub, moved) == date(2027, 2, 28)

    @pytest.mark.parametrize(
        "old_days, new_days, start, old_next, moved, following",
        [
            # The 1st, Monday only: Tuesday 1 June moved to Monday the 7th. Tuesday and Wednesday replace Monday: the
            # delivery moves on to Tuesday the 8th, and July's is Tuesday 6 July, not August's.
            ((0,), (1, 2), date(2027, 6, 1), date(2027, 6, 7), date(2027, 6, 8), date(2027, 7, 6)),
            # Monday to Wednesday become Friday to Sunday: Friday 1 January had moved to Monday the 4th; it moves on to
            # Friday the 8th, and February's is Friday 5 February, not March's.
            ((0, 1, 2), (4, 5, 6), date(2027, 1, 1), date(2027, 1, 4), date(2027, 1, 8), date(2027, 2, 5)),
        ],
        ids=["monday-to-tuesday-wednesday", "monday-wednesday-to-friday-sunday"],
    )
    def test_a_delivery_moved_on_more_than_a_week_after_its_date_is_still_that_months(
        self, db, old_days, new_days, start, old_next, moved, following
    ):
        """The move to the next delivery day can land up to 12 days after the month's date. The next delivery is
        still the next month's: it is counted from the month's date, not a month on from the move."""
        [slot] = _offer(db, old_days)
        sub = _sub(MONTHLY, day_of_month=1)
        assert first_delivery_on_or_after(sub, start) == old_next
        slot.available_days = list(new_days)
        db.session.commit()
        assert delivery_after_days_changed(sub, old_next) == moved
        assert advance(sub, moved) == following
        assert subscription_schedule.next_deliveries(sub, moved, 3)[1] == following
        assert subscription_schedule.catch_up(sub, moved, moved + timedelta(days=1)) == (following, [moved])

    def test_every_change_of_delivery_days_is_never_earlier_and_keeps_the_cycle(self, monkeypatch):
        """Every set of delivery days and every other set it can become, days added, removed or both. Every pair of
        sets runs, so one month's date already meets every weekday pattern; the dates are picked for the month lengths
        and clamps: the 1st of a 28-day and of a 30-day month, the 31st of January (February's is the 28th) and 29
        February in a leap year. The sweep replaces only the module's one database read (`delivery_weekdays`, whose
        rule over the slot rows is `weekdays_offered`) so it can run every pair; the examples above change real slot
        rows."""
        days_now = {"days": frozenset(range(7))}
        monkeypatch.setattr(subscription_schedule, "delivery_weekdays", lambda: days_now["days"])
        months = ((1, date(2027, 2, 1)), (1, date(2027, 6, 1)), (31, date(2027, 1, 1)), (29, date(2028, 2, 1)))
        moved_past_its_date = 0
        for old in map(frozenset, _all_day_sets()):
            days_now["days"] = old
            dated = [
                (sub, first_delivery_on_or_after(sub, start))
                for sub, start in ((_sub(MONTHLY, day_of_month=day), start) for day, start in months)
            ]
            for new in map(frozenset, _all_day_sets()):
                if new == old:
                    continue
                days_now["days"] = new
                for sub, old_next in dated:
                    case = (sorted(old), sorted(new), sub.delivery_day_of_month, old_next)
                    result = delivery_after_days_changed(sub, old_next)
                    assert result >= old_next and result.weekday() in new, case
                    assert advance(sub, result) == first_delivery_on_or_after(sub, result + timedelta(days=1)), case
                    month_date = subscription_schedule._target_on_or_before(old_next, sub.delivery_day_of_month)
                    moved_past_its_date += result - month_date > timedelta(days=6)
        # The sweep reaches the move that lands more than six days after the month's date, the case the examples pin.
        assert moved_past_its_date > 0

    def test_every_added_delivery_day_is_never_earlier_and_keeps_the_cycle(self, db):
        [slot] = _offer(db, [0])
        starts = (date(2027, 1, 1), date(2027, 2, 1), date(2028, 2, 1))
        subs = [_sub(MONTHLY, day_of_month=day) for day in (1, 29, 31)]
        for days in _all_day_sets():
            slot.available_days = list(days)
            db.session.commit()
            dated = [(sub, first_delivery_on_or_after(sub, start)) for sub in subs for start in starts]
            for added in sorted(set(range(7)) - set(days)):
                slot.available_days = sorted({*days, added})
                db.session.commit()
                for sub, old_next in dated:
                    case = (days, added, sub.delivery_day_of_month, old_next)
                    result = delivery_after_days_changed(sub, old_next)
                    assert result >= old_next, case
                    assert advance(sub, result) == first_delivery_on_or_after(sub, result + timedelta(days=1)), case
