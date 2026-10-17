"""The reorder-reminder rule, table by table (spec §4, §5, §7.1; D11, D12, D18, D19, D21).

Pure: every customer is constructed from the frozen facts, never loaded, so each branch of the
one rule is pinned without a database.
"""

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from business_app.services.reorder_reminders.config import ReminderConfig
from business_app.services.reorder_reminders.eligibility import (
    ChannelDecision,
    channel_for,
    days_since,
    evaluate,
    is_in_progress,
)
from business_app.services.reorder_reminders.facts import LiveOrderFacts, MemberFacts, ReminderCustomer
from business_app.services.reorder_reminders.settings import DEFAULT_SETTING, EffectiveSetting
from shared.business_config import REORDER_REMINDER_DEFAULTS

pytestmark = pytest.mark.unit

TZ = ZoneInfo("Asia/Tashkent")
DAY0 = date(2026, 3, 2)
CFG = ReminderConfig.from_mapping(REORDER_REMINDER_DEFAULTS)


def day(n):
    return DAY0 + timedelta(days=n)


def at(n, hour=12):
    return datetime.combine(day(n), time(hour), tzinfo=TZ)


def member(user_id=1, **overrides):
    fields = dict(
        user_id=user_id, status="active", is_staff=False, can_order=True,
        telegram_id=str(770000000 + user_id), is_bot_active=True, telegram_blocked_at=None,
        last_bot_interaction=at(-1), email=None, created_at=at(-400), preferred_language="uz",
        greeting_name="Ali",
    )
    fields.update(overrides)
    return MemberFacts(**fields)


def live(order_id, *, created, delivery_date=None, awaiting=False, own=True, user_id=1):
    return LiveOrderFacts(
        order_id=order_id, user_id=user_id, delivery_address_id=10, created_on=day(created),
        delivery_date=delivery_date, awaiting_new_date=awaiting, is_own=own,
    )


def customer(deliveries=(-21, -14, -7, 0), *, members=None, supply=None, live_orders=(), subscription=False,
             outlet=False, setting=DEFAULT_SETTING):
    members = tuple(members or (member(),))
    own = tuple(sorted(((100 + index, at(n)) for index, n in enumerate(deliveries)), key=lambda d: d[1], reverse=True))
    return ReminderCustomer(
        key=f"u:{members[0].user_id}", member_ids=tuple(m.user_id for m in members), members=members,
        own_deliveries=own, supply=supply or (own[0] if own else None), place_group_ids=(),
        live_orders=tuple(live_orders), has_live_subscription=subscription, has_any_outlet=outlet,
        dormant_outlets=(), setting=setting,
    )


ADMIN_OFF = EffectiveSetting(enabled=False, changed_by_kind="admin", changed_via="admin_ui", changed_at=at(1),
                             reason="Moved abroad", source_user_id=1)
CUSTOMER_OFF = EffectiveSetting(enabled=False, changed_by_kind="customer", changed_via="telegram_button",
                                changed_at=at(12, 10), reason=None, source_user_id=1)

# Spec §4 "Default ladder", the R1 column: the due day D.
LADDER = [
    ("weekly", (-21, -14, -7, 0), 7, Decimal("10.5"), 11),
    ("every two weeks", (-42, -28, -14, 0), 14, Decimal("21"), 22),
    ("monthly", (-90, -60, -30, 0), 30, Decimal("45"), 46),
    ("preset: one delivery day", (0,), None, Decimal("30"), 31),
    ("preset: two delivery days are still too few", (-10, 0), None, Decimal("30"), 31),
]


@pytest.mark.parametrize("label, deliveries, gap, threshold, due", LADDER, ids=[row[0] for row in LADDER])
def test_the_due_day_is_the_supply_day_plus_the_floor_of_the_threshold_plus_one(label, deliveries, gap, threshold, due):
    facts = customer(deliveries)

    before = evaluate(facts, today=day(due - 1), cfg=CFG)
    on_day = evaluate(facts, today=day(due), cfg=CFG)

    assert (on_day.usual_gap, on_day.threshold, on_day.due_on) == (gap, threshold, day(due))
    assert (before.due, on_day.due, on_day.fresh) == (False, True, True)


DAYS_SINCE = [
    ("noon the same local day", at(11), 0),
    ("noon the day before", at(10), 1),
    ("23:30 local the evening before", datetime.combine(day(10), time(23, 30), tzinfo=TZ), 1),
    ("00:30 local the day before is 19:30 UTC two days back", datetime.combine(day(10), time(0, 30), tzinfo=TZ), 1),
    ("a naive instant is UTC: 19:30Z is 00:30 local today", datetime(2026, 3, 12, 19, 30), 0),
    ("thirty days back", at(-19), 30),
]


@pytest.mark.parametrize("label, supply_instant, expected", DAYS_SINCE, ids=[row[0] for row in DAYS_SINCE])
def test_days_since_counts_local_calendar_days(label, supply_instant, expected):
    """Contract amendment 8: the one "days since" rule, which the payload, the email and the
    worklist read. Local days, so a delivery late in the evening is not a day older in UTC."""
    assert days_since(supply_instant, day(11)) == expected


def test_a_lapse_is_due_once_the_days_since_its_supply_exceed_the_threshold():
    """Weekly customer, threshold 10.5: day 10 is 10 days on (not due), day 11 is 11 (due)."""
    facts = customer()

    assert [days_since(facts.supply[1], day(n)) for n in (10, 11)] == [10, 11]
    assert [evaluate(facts, today=day(n), cfg=CFG).due for n in (10, 11)] == [False, True]


def test_two_deliveries_on_one_day_are_one_delivery_day():
    """D6: same-day deliveries count once, so three delivery days still make a personal rhythm."""
    own = ((104, at(0, 15)), (103, at(0, 9)), (102, at(-7)), (101, at(-14)))
    facts = replace(customer(), own_deliveries=own, supply=own[0])

    evaluation = evaluate(facts, today=day(11), cfg=CFG)

    assert (evaluation.usual_gap, evaluation.due_on) == (7, day(11))


def test_a_lapse_is_fresh_for_fresh_days_after_its_due_day_then_long_lapsed():
    """D11."""
    facts = customer()

    assert evaluate(facts, today=day(11 + 14), cfg=CFG).fresh is True
    late = evaluate(facts, today=day(11 + 15), cfg=CFG)
    assert (late.due, late.fresh) == (True, False)


PROGRESS = [
    ("created today", live(1, created=11), True),
    ("created grace_days ago", live(1, created=4), True),
    ("created grace_days + 1 ago", live(1, created=3), False),
    ("booked ahead and checked on its delivery day", live(1, created=-4, delivery_date=day(11)), True),
    ("a PENDING future-dated order", live(1, created=9, delivery_date=day(20)), True),
    ("dated yesterday and still live", live(1, created=-4, delivery_date=day(10)), False),
    ("awaiting a new date after a failed delivery", live(1, created=10, awaiting=True), False),
]


@pytest.mark.parametrize("label, order, expected", PROGRESS, ids=[row[0] for row in PROGRESS])
def test_an_order_is_in_progress_or_stuck(label, order, expected):
    """D12, spec §4 "In-progress live order"."""
    assert is_in_progress(order, today=day(11), grace_days=7) is expected


PAUSES = [
    ("my own order on its way pauses me", [live(1, created=10)], True, False),
    ("my own orders all stuck escalate me", [live(1, created=-10)], False, True),
    ("a coworker's order on its way to the place pauses me (D18)",
     [live(1, created=-10), live(2, created=10, own=False, user_id=2)], True, False),
    ("a coworker's stuck order neither pauses nor escalates me (D18)",
     [live(2, created=-10, own=False, user_id=2)], False, False),
    ("one own order on its way outweighs an own stuck one", [live(1, created=-10), live(3, created=10)], True, False),
    ("no live order", [], False, False),
]


@pytest.mark.parametrize("label, orders, paused, stuck", PAUSES, ids=[row[0] for row in PAUSES])
def test_pausing_and_stuck_orders(label, orders, paused, stuck):
    evaluation = evaluate(customer(live_orders=orders), today=day(11), cfg=CFG)

    assert (evaluation.paused, evaluation.own_live_all_stuck) == (paused, stuck)


SCOPE = [
    ("in scope", customer(), "customer", True, None),
    ("E3: no own delivery", customer(()), "none", False, "no_deliveries"),
    ("E6: admin-off, even with an outlet", customer(setting=ADMIN_OFF, outlet=True), "none", False, "admin_off"),
    ("E4: a live subscription", customer(subscription=True), "none", False, "subscription"),
    ("E1: every member is staff", customer(members=[member(1, is_staff=True)]), "none", False, "staff"),
    ("E1: no member is active", customer(members=[member(1, status="inactive")]), "none", False, "inactive"),
    ("E2: no member can order", customer(members=[member(1, can_order=False)]), "none", False, "no_recipient"),
    ("E5/D21: an outlet in any stage takes the outlet path", customer(outlet=True), "outlet", False, "outlet"),
    ("D5: customer-off is not an exclusion", customer(setting=CUSTOMER_OFF), "customer", True, None),
]


@pytest.mark.parametrize("label, facts, path, in_scope, reason", SCOPE, ids=[row[0] for row in SCOPE])
def test_scope(label, facts, path, in_scope, reason):
    evaluation = evaluate(facts, today=day(11), cfg=CFG)

    assert (evaluation.path, evaluation.in_scope, evaluation.out_of_scope_reason) == (path, in_scope, reason)


CHANNELS = [
    ("connected Telegram", customer(), ChannelDecision("telegram", None)),
    ("a real email", customer(members=[member(1, telegram_id=None, is_bot_active=False, email="ali@example.com")]),
     ChannelDecision("email", None)),
    ("a placeholder email is no channel",
     customer(members=[member(1, telegram_id=None, is_bot_active=False, email="telegram_77@bot.internal")]),
     ChannelDecision(None, "no_channel")),
    ("customer-off stops every channel", customer(setting=CUSTOMER_OFF), ChannelDecision(None, "stopped")),
    ("D19: the only account blocked the bot",
     customer(members=[member(1, is_bot_active=False, telegram_blocked_at=at(5), email="ali@example.com")]),
     ChannelDecision(None, "blocked")),
    ("D19: a blocked account's linked sibling is reminded on its own channel",
     customer(members=[member(1, is_bot_active=False, telegram_blocked_at=at(5)),
                       member(2, telegram_id=None, is_bot_active=False, email="work@example.com")]),
     ChannelDecision("email", None)),
]


@pytest.mark.parametrize("label, facts, expected", CHANNELS, ids=[row[0] for row in CHANNELS])
def test_the_reminder_channel(label, facts, expected):
    evaluation = evaluate(facts, today=day(11), cfg=CFG)

    assert evaluation.channel == expected
    assert channel_for(facts, evaluation.recipient) == expected


def test_the_blocked_sibling_is_never_the_recipient_while_another_account_can_be_reached():
    facts = customer(members=[member(1, is_bot_active=False, telegram_blocked_at=at(5)),
                              member(2, telegram_id=None, is_bot_active=False, email="work@example.com")])

    assert evaluate(facts, today=day(11), cfg=CFG).recipient.user_id == 2


def test_contact_restriction_is_published_from_the_current_setting_and_block_state():
    stopped = evaluate(customer(setting=CUSTOMER_OFF), today=day(25), cfg=CFG)
    blocked = evaluate(customer(members=[member(1, is_bot_active=False, telegram_blocked_at=at(5))]), today=day(25), cfg=CFG)

    assert stopped.contact_restriction == {"kind": "stopped", "since": at(12, 10).isoformat(), "via": "telegram_button"}
    assert blocked.contact_restriction == {"kind": "blocked", "since": at(5).isoformat(), "via": None}
    assert evaluate(customer(), today=day(25), cfg=CFG).contact_restriction is None


def test_the_place_group_example_alice_and_bob():
    """Spec §4 "Place group example (D18)": Alice (own rhythm 7 days) delivers to the office on
    day 0, which restarts both clocks; Bob keeps his own 30-day rhythm."""
    alice = customer((-21, -14, -7, 0))
    alice_delivery = alice.own_anchor
    bob = customer((-150, -120, -90, -60), members=[member(2)], supply=alice_delivery)

    assert evaluate(alice, today=day(11), cfg=CFG).due_on == day(11)
    assert evaluate(bob, today=day(45), cfg=CFG).due is False
    bob_on_46 = evaluate(bob, today=day(46), cfg=CFG)
    assert (bob_on_46.usual_gap, bob_on_46.due_on, bob_on_46.due, bob_on_46.own_anchor[1]) == (30, day(46), True, at(-60))

    # Bob orders for the office on day 12: Alice's supply anchor moves and her clock restarts.
    alice_after = replace(alice, supply=(300, at(12)))
    assert evaluate(alice_after, today=day(13), cfg=CFG).due_on == day(23)


def test_a_member_who_never_ordered_is_out_of_scope_even_with_the_place_supplied():
    """E3: group members who never ordered are never reminded, whatever their place's supply."""
    eve = customer((), members=[member(5)], supply=(300, at(0)))

    assert evaluate(eve, today=day(40), cfg=CFG).out_of_scope_reason == "no_deliveries"
