"""One frequency, a chosen day and a chosen time slot: the customer bot's subscription screens
(plan 2026-10-24 Task 4; spec 2026-10-23 §2.9 and §9).

Walked through the REAL dispatcher with the exact bodies the bot sends to the backend:

* creation sends ONE ``frequency`` (never ``billing_cycle`` / ``delivery_frequency``), a weekday
  (0 = Monday) for weekly and biweekly, a day of the month for monthly, and a slot for every frequency;
* there is no templates screen and no retry-billing button;
* the card and the list show the backend's ``upcoming_delivery`` and seeded labels, never the bare
  ``frequency_weekly`` key or the raw ``weekly`` enum, and never "next billing";
* "change frequency" persists, with the day a weekly, biweekly or monthly cadence needs.

The bot does no date maths: the slots come from ``GET /delivery/time-slots?weekday=`` and the dates it
prints are the ones the backend published.
"""

import pytest
from telegram.ext import ConversationHandler

from handlers.subscriptions import (
    CONFIRM_SUBSCRIPTION,
    SELECT_ADDRESS,
    SELECT_DAY,
    SELECT_PAYMENT,
    SELECT_QUANTITY,
    SELECT_SLOT,
)
from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, backend_failure, build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SUBSCRIPTIONS = "/api/v1/subscriptions"
SLOTS = "/api/v1/delivery/time-slots"
SUB_ID = 12
PRODUCT_ID = 7
ADDRESS_ID = 91
MORNING, EVENING = 5, 6

TRANSLATIONS = {
    "telegram.subscription.select_products": "PICK-A-PRODUCT",
    "telegram.subscription.select_quantity": "HOW-MANY",
    "telegram.subscription.select_frequency": "HOW-OFTEN",
    "telegram.subscription.select_day_of_week": "WHICH-WEEKDAY",
    "telegram.subscription.select_day_of_month": "WHICH-MONTH-DAY",
    "telegram.subscription.select_time_slot": "WHICH-TIME",
    "telegram.subscription.any_time": "ANY-TIME",
    "telegram.subscription.select_address": "WHERE-TO",
    "telegram.subscription.select_payment": "HOW-TO-PAY",
    "telegram.subscription.select_new_frequency": "PICK-NEW-FREQUENCY",
    "telegram.subscription.select_new_time_slot": "PICK-NEW-SLOT",
    "telegram.subscription.preview_title": "PREVIEW-TITLE",
    "telegram.subscription.save_change": "SAVE",
    "telegram.subscription.schedule_updated_successfully": "SCHEDULE-UPDATED",
    "telegram.subscription.confirm_title": "CONFIRM-TITLE",
    "telegram.subscription.created_success": "CREATED",
    "telegram.subscription.details_title": "DETAILS",
    "telegram.subscription.status": "Status",
    "telegram.subscription.frequency": "Frequency",
    "telegram.subscription.next_delivery": "Next delivery",
    "telegram.subscription.delivery_day": "Delivery day",
    "telegram.subscription.delivery_time": "Delivery time",
    "telegram.subscription.time_to_be_confirmed": "time to be confirmed",
    "telegram.subscription.items": "Items",
    "telegram.subscription.total": "Total",
    "telegram.subscription.id": "ID",
    "telegram.subscription.title": "SUBSCRIPTIONS",
    "telegram.subscription.active": "Active",
    "telegram.subscription.paused": "Paused",
    "telegram.subscription.frequency_daily": "Daily",
    "telegram.subscription.frequency_weekly": "Weekly",
    "telegram.subscription.frequency_biweekly": "Every 2 weeks",
    "telegram.subscription.frequency_monthly": "Monthly",
    "telegram.subscription.status_active": "Active",
    "telegram.subscription.status_paused": "Paused",
    "telegram.subscription.weekday_0": "Monday",
    "telegram.subscription.weekday_1": "Tuesday",
    "telegram.subscription.weekday_2": "Wednesday",
    "telegram.subscription.weekday_3": "Thursday",
    "telegram.subscription.weekday_4": "Friday",
    "telegram.subscription.weekday_5": "Saturday",
    "telegram.subscription.weekday_6": "Sunday",
    "telegram.currency.uzs": "UZS",
    "telegram.back": "Back",
    "telegram.cancel": "Cancel",
    "telegram.continue": "Continue",
    "telegram.confirm": "Confirm",
}

SLOT_ROWS = [
    {"id": MORNING, "name": "Morning", "start_time": "10:00", "end_time": "12:00",
     "time_range": "10:00-12:00", "available_days": [0, 1, 2, 3, 4, 5, 6]},
    {"id": EVENING, "name": "Evening", "start_time": "18:00", "end_time": "20:00",
     "time_range": "18:00-20:00", "available_days": [0, 1, 2, 3, 4]},
]
ITEMS = [{"product_id": PRODUCT_ID, "quantity": 2}]


def subscription(**overrides):
    base = {
        "id": SUB_ID,
        "name": "Weekly water",
        "status": "active",
        "frequency": "weekly",
        "billing_amount": 50000.0,
        "next_delivery_date": "2026-10-29",
        "schedule_sentence": "Every Thursday, 10:00–12:00",
        "upcoming_delivery": {"date": "2026-10-29", "time_label": "10:00–12:00", "order_id": None},
    }
    base.update(overrides)
    return base


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    backend = harness.backend
    # The picker reads the subscription catalog (`GET /subscriptions/products`, re-review G6).
    backend.route(
        "GET", "/api/v1/subscriptions/products",
        lambda _c: {"data": {"items": [
            {"id": PRODUCT_ID, "product_name": "Aqua 19L", "unit_price_label": "25 000 so'm"},
        ]}},
    )
    backend.route(
        "GET", f"/api/v1/products/{PRODUCT_ID}",
        lambda _c: {"data": {"product": {
            "id": PRODUCT_ID, "name": "Aqua 19L", "pricing": {"base_price": 25000},
            "inventory": {"stock_quantity": 500, "track_inventory": True, "is_in_stock": True,
                          "min_order_quantity": 2},
        }}},
    )
    backend.route("GET", SLOTS, lambda _c: {"weekday": "x", "time_slots": SLOT_ROWS})
    backend.route(
        "GET", "/api/v1/payments/methods",
        lambda _c: {"data": {"available_methods": [{"method": "cash", "is_active": True}]}},
    )
    backend.route(
        "POST", f"{SUBSCRIPTIONS}/preview",
        lambda c: {"data": {"preview": {
            "items": [{"product_name": "Aqua 19L", "quantity": 2, "unit_price_label": "25 000 so'm",
                       "line_total_label": "50 000 so'm"}],
            "frequency": c.data["frequency"], "total_amount": 50000.0, "total_amount_label": "50 000 so'm",
            "deliveries_per_month": 4.33,
        }}},
    )
    backend.route(
        "POST", SUBSCRIPTIONS,
        lambda _c: {"data": {"subscription": subscription()}},
    )
    backend.route("GET", f"{SUBSCRIPTIONS}/{SUB_ID}", lambda _c: {"data": {"subscription": subscription()}})
    backend.route("GET", f"{SUBSCRIPTIONS}/{SUB_ID}/items", lambda _c: {"data": {"items": []}})
    backend.addresses[ADDRESS_ID] = {"id": ADDRESS_ID, "address_line1": "15 Chilonzor", "is_default": True}
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


def texts(bot):
    return [call.text for call in bot.telegram.shown if call.text]


def last_text(bot):
    shown = texts(bot)
    assert shown, "the bot showed the customer nothing at all"
    return shown[-1]


def buttons(bot):
    markup = bot.telegram.last_shown().reply_markup or {}
    return [b.get("callback_data") for row in markup.get("inline_keyboard", []) for b in row]


def labels(bot):
    markup = bot.telegram.last_shown().reply_markup or {}
    return [b.get("text") for row in markup.get("inline_keyboard", []) for b in row]


def calls(bot, method, endpoint):
    return [c for c in bot.backend.calls if c.method == method and c.endpoint == endpoint]


def endpoints(bot):
    return [c.endpoint for c in bot.backend.calls]


def creation_state(bot):
    return bot.conversation_state("subscription_creation")


async def reach_frequency_screen(bot, user):
    await bot.send(user.tap("create_subscription"))
    await bot.send(user.tap(f"sub_product_{PRODUCT_ID}"))
    await bot.send(user.tap("sub_qty_2"))
    await bot.send(user.tap("sub_items_done"))
    assert last_text(bot) == "HOW-OFTEN"
    assert creation_state(bot) == SELECT_ADDRESS


async def finish_from_slot(bot, user, slot_callback):
    """From the slot screen to the created subscription."""
    await bot.send(user.tap(slot_callback))
    assert last_text(bot) == "WHERE-TO"
    await bot.send(user.tap(f"addr_{ADDRESS_ID}"))
    await bot.send(user.tap("sub_payment_cash"))
    assert creation_state(bot) == CONFIRM_SUBSCRIPTION
    await bot.send(user.tap("confirm_create_subscription"))


def created_body(bot):
    (call,) = calls(bot, "POST", SUBSCRIPTIONS)
    return call.data


# ---------------------------------------------------------------------------
# Creation: no templates screen, one frequency, a day, a slot
# ---------------------------------------------------------------------------


async def test_creation_goes_straight_to_the_products_with_no_templates_screen(bot, user):
    await bot.send(user.tap("create_subscription"))

    assert last_text(bot).startswith("PICK-A-PRODUCT")
    assert creation_state(bot) == SELECT_QUANTITY
    assert f"{SUBSCRIPTIONS}/templates" not in endpoints(bot)
    assert "subscription_custom" not in buttons(bot) and "subscription_use_template" not in buttons(bot)


async def test_a_weekly_subscription_sends_one_frequency_the_weekday_and_the_slot(bot, user):
    await reach_frequency_screen(bot, user)

    await bot.send(user.tap("subscription_freq_weekly"))
    assert last_text(bot) == "WHICH-WEEKDAY"
    assert creation_state(bot) == SELECT_DAY
    assert [b for b in buttons(bot) if b.startswith("subscription_day_")] == [
        f"subscription_day_{n}" for n in range(7)
    ]
    assert labels(bot)[:7] == ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    await bot.send(user.tap("subscription_day_3"))  # Thursday, 0 = Monday
    assert last_text(bot) == "WHICH-TIME"
    assert creation_state(bot) == SELECT_SLOT
    assert calls(bot, "GET", SLOTS)[-1].params == {"weekday": 3, "frequency": "weekly"}
    assert [b for b in buttons(bot) if b.startswith("subscription_slot_")] == [
        f"subscription_slot_{MORNING}", f"subscription_slot_{EVENING}", "subscription_slot_any",
    ]
    assert "10:00-12:00" in " ".join(labels(bot)) and "ANY-TIME" in labels(bot)

    await finish_from_slot(bot, user, f"subscription_slot_{EVENING}")

    assert calls(bot, "POST", f"{SUBSCRIPTIONS}/preview")[0].data == {"frequency": "weekly", "items": ITEMS}
    assert created_body(bot) == {
        "name": "Subscription - Weekly",
        "frequency": "weekly",
        "delivery_day_of_week": 3,
        "delivery_time_slot_id": EVENING,
        "delivery_address_id": ADDRESS_ID,
        "payment_method": "cash",
        "items": ITEMS,
        "auto_renew": True,
    }


async def test_a_biweekly_subscription_asks_the_weekday_too(bot, user):
    await reach_frequency_screen(bot, user)
    await bot.send(user.tap("subscription_freq_biweekly"))
    assert last_text(bot) == "WHICH-WEEKDAY"
    await bot.send(user.tap("subscription_day_0"))  # Monday is 0, not the JS 1

    await finish_from_slot(bot, user, f"subscription_slot_{MORNING}")

    body = created_body(bot)
    assert body["frequency"] == "biweekly" and body["delivery_day_of_week"] == 0
    assert "delivery_day_of_month" not in body


async def test_a_monthly_subscription_asks_the_day_of_the_month_and_offers_every_slot(bot, user):
    await reach_frequency_screen(bot, user)
    await bot.send(user.tap("subscription_freq_monthly"))
    assert last_text(bot) == "WHICH-MONTH-DAY"
    assert [b for b in buttons(bot) if b.startswith("subscription_day_")] == [
        f"subscription_day_{n}" for n in range(1, 32)
    ]

    await bot.send(user.tap("subscription_day_31"))
    assert calls(bot, "GET", SLOTS)[-1].params == {"weekday": "any", "frequency": "monthly"}
    await finish_from_slot(bot, user, f"subscription_slot_{MORNING}")

    body = created_body(bot)
    assert body["frequency"] == "monthly" and body["delivery_day_of_month"] == 31
    assert "delivery_day_of_week" not in body


async def test_a_daily_subscription_has_no_day_picker_but_still_picks_a_slot(bot, user):
    await reach_frequency_screen(bot, user)

    await bot.send(user.tap("subscription_freq_daily"))
    assert last_text(bot) == "WHICH-TIME"
    assert creation_state(bot) == SELECT_SLOT
    assert calls(bot, "GET", SLOTS)[-1].params == {"weekday": "any", "frequency": "daily"}
    await finish_from_slot(bot, user, f"subscription_slot_{MORNING}")

    body = created_body(bot)
    assert body["frequency"] == "daily" and body["delivery_time_slot_id"] == MORNING
    assert "delivery_day_of_week" not in body and "delivery_day_of_month" not in body


async def test_any_time_sends_no_slot(bot, user):
    await reach_frequency_screen(bot, user)
    await bot.send(user.tap("subscription_freq_weekly"))
    await bot.send(user.tap("subscription_day_2"))

    await finish_from_slot(bot, user, "subscription_slot_any")

    body = created_body(bot)
    assert "delivery_time_slot_id" not in body
    assert body["delivery_day_of_week"] == 2


async def test_no_slot_offered_that_day_skips_the_slot_screen(bot, user):
    bot.backend.route("GET", SLOTS, lambda _c: {"weekday": "6", "time_slots": []})
    await reach_frequency_screen(bot, user)
    await bot.send(user.tap("subscription_freq_weekly"))

    await bot.send(user.tap("subscription_day_6"))

    assert last_text(bot) == "WHERE-TO"
    assert creation_state(bot) == SELECT_PAYMENT
    await bot.send(user.tap(f"addr_{ADDRESS_ID}"))
    await bot.send(user.tap("sub_payment_cash"))
    await bot.send(user.tap("confirm_create_subscription"))
    body = created_body(bot)
    assert body["delivery_day_of_week"] == 6 and "delivery_time_slot_id" not in body


async def test_a_failed_slot_lookup_is_shown_not_swallowed(bot, user):
    bot.backend.route("GET", SLOTS, lambda _c: backend_failure("Slots unavailable", 500))
    await reach_frequency_screen(bot, user)
    await bot.send(user.tap("subscription_freq_weekly"))
    bot.telegram.reset()

    await bot.send(user.tap("subscription_day_1"))

    assert any("Slots unavailable" in (c.text or "") for c in bot.telegram.shown + bot.telegram.of("answerCallbackQuery"))
    assert not calls(bot, "POST", SUBSCRIPTIONS)


async def test_the_confirm_screen_names_the_frequency_day_and_time(bot, user):
    await reach_frequency_screen(bot, user)
    await bot.send(user.tap("subscription_freq_weekly"))
    await bot.send(user.tap("subscription_day_3"))
    await bot.send(user.tap(f"subscription_slot_{EVENING}"))
    await bot.send(user.tap(f"addr_{ADDRESS_ID}"))

    await bot.send(user.tap("sub_payment_cash"))

    text = last_text(bot)
    assert "Frequency: Weekly" in text
    assert "Delivery day: Thursday" in text
    assert "Delivery time: 18:00-20:00" in text
    assert "Total: 50 000 so'm" in text  # the quote's published label (re-review admin-bot-backend-4)


async def test_the_created_screen_shows_the_backends_upcoming_delivery(bot, user):
    await reach_frequency_screen(bot, user)
    await bot.send(user.tap("subscription_freq_weekly"))
    await bot.send(user.tap("subscription_day_3"))
    await finish_from_slot(bot, user, f"subscription_slot_{MORNING}")

    text = last_text(bot)
    assert "CREATED" in text
    assert "Next delivery: 29.10.2026, 10:00–12:00" in text
    assert f"subscription_{SUB_ID}" in buttons(bot)


# ---------------------------------------------------------------------------
# Back buttons on the two new screens
# ---------------------------------------------------------------------------


async def test_back_from_the_day_picker_returns_to_the_frequency_screen(bot, user):
    await reach_frequency_screen(bot, user)
    await bot.send(user.tap("subscription_freq_weekly"))
    assert "back_to_sub_frequency" in buttons(bot)

    await bot.send(user.tap("back_to_sub_frequency"))

    assert last_text(bot) == "HOW-OFTEN"
    assert creation_state(bot) == SELECT_ADDRESS
    await bot.send(user.tap("subscription_freq_monthly"))  # a different choice is honoured
    assert last_text(bot) == "WHICH-MONTH-DAY"


async def test_back_from_the_slot_picker_returns_to_the_day_picker(bot, user):
    await reach_frequency_screen(bot, user)
    await bot.send(user.tap("subscription_freq_weekly"))
    await bot.send(user.tap("subscription_day_3"))
    assert "back_to_sub_day" in buttons(bot)

    await bot.send(user.tap("back_to_sub_day"))

    assert last_text(bot) == "WHICH-WEEKDAY"
    assert creation_state(bot) == SELECT_DAY
    await bot.send(user.tap("subscription_day_4"))
    await finish_from_slot(bot, user, f"subscription_slot_{MORNING}")
    assert created_body(bot)["delivery_day_of_week"] == 4


async def test_back_from_a_daily_slot_picker_returns_to_the_frequency_screen(bot, user):
    await reach_frequency_screen(bot, user)
    await bot.send(user.tap("subscription_freq_daily"))

    await bot.send(user.tap("back_to_sub_day"))

    assert last_text(bot) == "HOW-OFTEN"
    assert creation_state(bot) == SELECT_ADDRESS


async def test_choosing_a_new_frequency_forgets_the_old_day(bot, user):
    await reach_frequency_screen(bot, user)
    await bot.send(user.tap("subscription_freq_weekly"))
    await bot.send(user.tap("subscription_day_3"))
    await bot.send(user.tap("back_to_sub_day"))
    await bot.send(user.tap("back_to_sub_frequency"))
    await bot.send(user.tap("subscription_freq_daily"))

    await finish_from_slot(bot, user, f"subscription_slot_{MORNING}")

    assert "delivery_day_of_week" not in created_body(bot)


# ---------------------------------------------------------------------------
# The card and the list
# ---------------------------------------------------------------------------


async def test_the_card_shows_one_next_delivery_from_the_backend(bot, user):
    await bot.send(user.tap(f"subscription_{SUB_ID}"))

    text = last_text(bot)
    assert "Next delivery: 29.10.2026, 10:00–12:00" in text
    assert "2026-10-29" not in text  # dd.mm.yyyy, as the order notices print it, never the raw ISO date
    assert "📅 Every Thursday, 10:00–12:00" in text  # the backend's schedule sentence, not a bot-built frequency line
    assert "Status: Active" in text
    assert "Next billing" not in text and "2026-10-28" not in text
    assert "weekly" not in text and "frequency_weekly" not in text


async def test_a_delivery_without_a_window_prints_the_time_label_the_backend_published(bot, user):
    """"Time to be confirmed" and "Any time" are the backend's `time_label`; the bot adds no wording of its own."""
    bot.backend.route(
        f"GET", f"{SUBSCRIPTIONS}/{SUB_ID}",
        lambda _c: {"data": {"subscription": subscription(upcoming_delivery={
            "date": "2026-10-29", "time_label": "Time to be confirmed", "order_id": 301})}},
    )
    await bot.send(user.tap(f"subscription_{SUB_ID}"))

    assert "Next delivery: 29.10.2026, Time to be confirmed" in last_text(bot)


async def test_a_paused_subscription_has_no_next_delivery_line(bot, user):
    bot.backend.route(
        "GET", f"{SUBSCRIPTIONS}/{SUB_ID}",
        lambda _c: {"data": {"subscription": subscription(
            status="paused", upcoming_delivery=None, next_delivery_date="2026-10-29")}},
    )
    await bot.send(user.tap(f"subscription_{SUB_ID}"))

    text = last_text(bot)
    assert "Status: Paused" in text
    assert "Next delivery" not in text


@pytest.mark.parametrize("status", ["active", "paused", "cancelled"])
async def test_no_card_offers_a_retry_billing_button(bot, user, status):
    bot.backend.route(
        "GET", f"{SUBSCRIPTIONS}/{SUB_ID}",
        lambda _c: {"data": {"subscription": subscription(status=status)}},
    )
    await bot.send(user.tap(f"subscription_{SUB_ID}"))

    assert not [b for b in buttons(bot) if b.startswith("retry_billing")]


async def test_the_retry_billing_callback_is_no_longer_claimed(bot, user):
    assert bot.handlers_matching(user.tap(f"retry_billing_{SUB_ID}")) == []
    assert f"{SUBSCRIPTIONS}/{SUB_ID}/retry-billing" not in endpoints(bot)


async def test_the_list_labels_each_subscription_with_its_seeded_frequency(bot, user):
    bot.backend.route(
        "GET", SUBSCRIPTIONS,
        lambda _c: {"data": {"items": [
            subscription(id=1, name="Home", frequency="weekly"),
            subscription(id=2, name="Office", frequency="biweekly"),
        ]}},
    )
    await bot.send(user.tap("menu_subscriptions"))

    shown = labels(bot)
    assert "✅ Home - Weekly" in shown
    assert "✅ Office - Every 2 weeks" in shown


# ---------------------------------------------------------------------------
# Change frequency: the day, then the slot, then a preview, then one save (spec 2026-10-27 §6.2)
# ---------------------------------------------------------------------------

STATE = "0123456789abcdef"


def put_body(bot):
    (call,) = calls(bot, "PUT", f"{SUBSCRIPTIONS}/{SUB_ID}")
    return call.data


def preview_body(bot):
    (call,) = calls(bot, "POST", f"{SUBSCRIPTIONS}/{SUB_ID}/preview")
    return call.data


def answers(bot):
    return [a.params.get("text") for a in bot.telegram.visible_answers]


@pytest.fixture(autouse=True)
def the_backend_previews_a_schedule(bot):
    bot.backend.route(
        "POST", f"{SUBSCRIPTIONS}/{SUB_ID}/preview",
        lambda _c: {"data": {"preview": {
            "schedule_sentence": "SCHEDULE-SENTENCE", "sentence": "FIRST-DELIVERY", "expected_state": STATE,
            "upcoming_dates": [],
        }}},
    )
    bot.backend.route("PUT", f"{SUBSCRIPTIONS}/{SUB_ID}", lambda _c: {"data": {"subscription": subscription()}})


async def test_changing_to_daily_asks_the_slot_then_previews_then_saves(bot, user):
    await bot.send(user.tap(f"change_frequency_{SUB_ID}"))
    await bot.send(user.tap(f"subfreq_daily_{SUB_ID}"))

    assert not calls(bot, "PUT", f"{SUBSCRIPTIONS}/{SUB_ID}"), "persisted before the slot was chosen"
    assert last_text(bot) == "PICK-NEW-SLOT"
    assert calls(bot, "GET", SLOTS)[-1].params == {"weekday": "any", "frequency": "daily"}
    assert f"subsched_daily_x_{MORNING}_{SUB_ID}" in buttons(bot) and f"subsched_daily_x_any_{SUB_ID}" in buttons(bot)

    await bot.send(user.tap(f"subsched_daily_x_{MORNING}_{SUB_ID}"))

    assert preview_body(bot) == {"frequency": "daily", "delivery_time_slot_id": MORNING}
    assert not calls(bot, "PUT", f"{SUBSCRIPTIONS}/{SUB_ID}"), "the preview saved"
    assert buttons(bot)[0] == f"subschedok_daily_x_{MORNING}_{SUB_ID}_{STATE}"

    await bot.send(user.tap(f"subschedok_daily_x_{MORNING}_{SUB_ID}_{STATE}"))

    assert put_body(bot) == {"frequency": "daily", "delivery_time_slot_id": MORNING, "expected_state": STATE}
    assert "SCHEDULE-UPDATED" in answers(bot)


@pytest.mark.parametrize(
    "frequency, day, expected",
    [
        ("weekly", 2, {"frequency": "weekly", "delivery_day_of_week": 2}),
        ("biweekly", 0, {"frequency": "biweekly", "delivery_day_of_week": 0}),
        ("monthly", 15, {"frequency": "monthly", "delivery_day_of_month": 15}),
    ],
)
async def test_changing_to_a_dated_frequency_asks_the_day_then_the_slot_then_persists_all_three(
    bot, user, frequency, day, expected
):
    await bot.send(user.tap(f"change_frequency_{SUB_ID}"))

    await bot.send(user.tap(f"subfreq_{frequency}_{SUB_ID}"))

    assert not calls(bot, "PUT", f"{SUBSCRIPTIONS}/{SUB_ID}"), "persisted before the day was chosen"
    assert last_text(bot) == ("WHICH-MONTH-DAY" if frequency == "monthly" else "WHICH-WEEKDAY")
    assert f"subday_{frequency}_{day}_{SUB_ID}" in buttons(bot)

    bot.application.user_data[DEFAULT_USER_ID].clear()  # a deploy between the taps
    await bot.send(user.tap(f"subday_{frequency}_{day}_{SUB_ID}"))

    assert not calls(bot, "PUT", f"{SUBSCRIPTIONS}/{SUB_ID}"), "persisted before the slot was chosen"
    assert last_text(bot) == "PICK-NEW-SLOT"
    assert calls(bot, "GET", SLOTS)[-1].params == {
        "weekday": day if frequency != "monthly" else "any", "frequency": frequency,
    }
    slot_tap = f"subsched_{frequency}_{day}_{MORNING}_{SUB_ID}"
    assert slot_tap in buttons(bot)

    await bot.send(user.tap(slot_tap))
    assert preview_body(bot) == {**expected, "delivery_time_slot_id": MORNING}

    await bot.send(user.tap(f"subschedok_{frequency}_{day}_{MORNING}_{SUB_ID}_{STATE}"))
    assert put_body(bot) == {**expected, "delivery_time_slot_id": MORNING, "expected_state": STATE}
    assert "SCHEDULE-UPDATED" in answers(bot)


async def test_a_failed_schedule_change_is_shown_and_not_reported_as_saved(bot, user):
    bot.backend.route("PUT", f"{SUBSCRIPTIONS}/{SUB_ID}", lambda _c: backend_failure("Cannot change", 400))
    await bot.send(user.tap(f"subschedok_weekly_1_any_{SUB_ID}_{STATE}"))

    assert "SCHEDULE-UPDATED" not in answers(bot)
    assert "❌ Cannot change" in answers(bot)
