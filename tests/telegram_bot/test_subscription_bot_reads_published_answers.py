"""The bot's subscription screens read the backend's published answers (plan 2026-10-26, Task 5; spec §6).

Driven through the real PTB dispatcher. Only the three harness seams are faked; the keyboards, the callback
patterns and the real ``api_client`` endpoint paths and payloads are production code. The ``allowed_actions``
below have the exact shape ``SubscriptionService.allowed_actions`` publishes (Task 1).

What each test pins down:

* buttons come from ``allowed_actions`` (no status checks in the bot), per status;
* Pause and Cancel ask first with the consequence the backend published, and send the ``expected_state`` the card
  showed; Skip asks its reason, sends the date the button named and handles the 409 "this changed";
* the remove-item follow-up renders the subscription, not the item;
* the frequency screen's Back returns to the subscription;
* the payment menu is asked with the subscription's ``delivery_address_id``;
* a time slot can be changed, from the slots the backend filters;
* Statistics is gone (the order history is tested in test_subscription_card_and_history_dispatcher.py);
* every body the bot sends passes the backend's own request model (the route-compat check).
"""

import pytest

from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, backend_failure, build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SUB_ID = 12
ITEM_ID = 33
ADDRESS_ID = 77
SUBSCRIPTION = f"/api/v1/subscriptions/{SUB_ID}"
PAYMENT_METHODS = "/api/v1/payments/methods"
SLOTS = "/api/v1/delivery/time-slots"
SKIP_DATE = "2026-11-06"
STATE = "0123456789abcdef"  # an `allowed_actions.expected_state`: 16 hex characters

TRANSLATIONS = {
    "telegram.subscription.details_title": "DETAILS",
    "telegram.subscription.pause": "PAUSE",
    "telegram.subscription.resume": "RESUME",
    "telegram.subscription.skip_next": "SKIP",
    "telegram.subscription.edit": "EDIT",
    "telegram.subscription.manage_items": "ITEMS",
    "telegram.subscription.orders": "ORDERS",
    "telegram.orders.skip_no_reason": "NO-REASON",
    "telegram.subscription.cancelled_success": "CANCELLED-OK",
    "telegram.subscription.skip_success": "SKIPPED-OK",
    "telegram.subscription.skip_stale": "CHANGED-REFRESH",
    "telegram.subscription.paused_success": "PAUSED-OK",
    "telegram.subscription.resumed_success": "RESUMED-OK",
    "telegram.subscription.change_time_slot": "CHANGE-SLOT",
    "telegram.subscription.select_new_time_slot": "PICK-NEW-SLOT",
    "telegram.subscription.schedule_updated_successfully": "SCHEDULE-UPDATED",
    "telegram.subscription.items_updated_successfully": "ITEMS-UPDATED",
    "telegram.subscription.preview_title": "PREVIEW-TITLE",
    "telegram.subscription.save_change": "SAVE",
    "telegram.subscription.select_new_frequency": "PICK-NEW-FREQUENCY",
    "telegram.subscription.select_new_payment_method": "PICK-NEW-PAYMENT",
    "telegram.subscription.edit_menu": "EDIT-MENU",
    "telegram.subscription.add_item": "ADD-ITEM",
    "telegram.subscription.current_items": "CURRENT-ITEMS",
    "telegram.subscription.per_month": "PER-MONTH",
    "telegram.subscription.payment": "PAYMENT",
    "telegram.subscription.next_delivery": "NEXT",
    "telegram.subscription.next_dates": "DATES",
    "telegram.subscription.statistics": "STATS",
    "telegram.subscription.create": "CREATE",
    "telegram.subscription.title": "SUBSCRIPTIONS",
    "telegram.subscription.status": "Status",
    "telegram.subscription.status_active": "active",
    "telegram.subscription.status_paused": "paused",
    "telegram.subscription.status_cancelled": "cancelled",
    "telegram.subscription.frequency": "Frequency",
    "telegram.subscription.frequency_daily": "Daily",
    "telegram.subscription.frequency_weekly": "Weekly",
    "telegram.subscription.frequency_biweekly": "Biweekly",
    "telegram.subscription.frequency_monthly": "Monthly",
    "telegram.subscription.any_time": "ANY-TIME",
    "telegram.back": "Back",
    "telegram.subscription.items": "ITEMS-LABEL",
    "telegram.subscription.per_delivery": "PER-DELIVERY",
    "telegram.common.unknown": "UNKNOWN-PRODUCT",
    "telegram.subscription.item_not_included": "NOT-INCLUDED",
    "telegram.subscription.some_unavailable_note": "SOME-LEFT-OUT",
    "telegram.subscription.none_available_note": "NONE-LEFT",
    "telegram.subscription.select_products": "PICK-A-PRODUCT",
    "telegram.subscription.select_product_to_add": "PICK-A-PRODUCT-TO-ADD",
    "telegram.cancel": "Cancel",
}


LABELS = {
    "pay": "Pay 30 000", "skip": "Skip this delivery", "pause": "Pause", "resume": "Resume deliveries",
    "cancel": "Cancel subscription",
}
CANCEL_WORDS = {
    "confirm_text": "We won't place any more automatic orders.", "kept_lines": ["Your paid order stays."],
    "cancel_lines": ["We'll cancel your order for 6 November."], "coins_line": "Your AquaCoins stay with you.",
    "confirm_label": "Cancel subscription", "keep_label": "Keep subscription",
}
SKIP_WORDS = {
    "confirm_text": "Skip Friday, 6 November?", "keep_label": "Keep this delivery",
    "reasons": [{"code": "away", "label": "I'll be away"}, {"code": "other", "label": "Other"}],
}
PAUSE_WORDS = {
    "needs_confirm": True, "confirm_text": "Pause deliveries?", "kept_lines": [], "cancel_lines": ["We'll cancel 6 November."],
    "confirm_label": "Pause subscription", "keep_label": "Keep delivering",
}


def allowed(**actions):
    """`allowed_actions` as the backend publishes it: every action present, each `{allowed, block_reason, ...}`,
    an allowed one with its `label`, and the `expected_state` token the card's writes send back."""
    published = {
        name: {"allowed": False, "block_reason": "Not available right now.", "label": None}
        for name in ("pay", "skip", "pause", "resume", "cancel", "edit", "remove_item")
    }
    for name, extra in actions.items():
        published[name] = {"allowed": True, "block_reason": None, "label": LABELS.get(name), **(extra or {})}
    published["expected_state"] = STATE
    return published


ACTIVE = allowed(
    skip={"date": SKIP_DATE, "date_label": "6 November", "will_cancel_orders": [], "kept_orders": [], **SKIP_WORDS},
    pause={"will_cancel_orders": [], "kept_orders": [], **PAUSE_WORDS},
    cancel={"will_cancel_orders": [], "kept_orders": [], **CANCEL_WORDS},
    edit=None,
    remove_item=None,
)
PAUSED = allowed(
    resume={"resume_preview": "2026-11-13"}, cancel={"will_cancel_orders": [], "kept_orders": [], **CANCEL_WORDS}, edit=None,
)
CANCELLED = allowed()  # nothing is allowed on a cancelled subscription


def serve(bot, *, status="active", frequency="weekly", actions=ACTIVE, **extra):
    subscription = {
        "id": SUB_ID,
        "status": status,
        "frequency": frequency,
        "delivery_address_id": ADDRESS_ID,
        "delivery_day_of_week": 3,
        "schedule_sentence": "Every Thursday, 10:00–12:00",
        "allowed_actions": actions,
        # The items ride the card (`subscription_items`); the bot reads no second `GET .../items`.
        "subscription_items": [
            {"id": ITEM_ID, "product_id": 7, "quantity": 2, "unit_price": 12000, "product": {"name": "Aqua 19L"}},
        ],
        **extra,
    }
    bot.backend.route("GET", SUBSCRIPTION, lambda _c: {"data": {"subscription": subscription}})
    return subscription


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    serve(harness)
    harness.backend.route(
        "GET", PAYMENT_METHODS,
        lambda _c: {"data": {"available_methods": [{"method": "cash", "is_active": True}]}},
    )
    harness.backend.route(
        "GET", SLOTS,
        lambda _c: {"time_slots": [
            {"id": 5, "name": "Morning", "time_range": "10:00-12:00"},
            {"id": 6, "name": "Evening", "time_range": "18:00-20:00"},
        ]},
    )
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


def answers(bot):
    """Every callback answer's text, popups or not."""
    return [call.params.get("text") for call in bot.telegram.of("answerCallbackQuery")]


def alerts(bot):
    return [
        call.params.get("text")
        for call in bot.telegram.of("answerCallbackQuery")
        if call.params.get("show_alert")
    ]


# ---------------------------------------------------------------------------
# Buttons come from allowed_actions
# ---------------------------------------------------------------------------


async def test_an_active_subscription_offers_exactly_what_the_backend_allows(bot, user):
    await bot.send(user.tap(f"subscription_{SUB_ID}"))

    assert buttons(bot) == [
        f"skip_sub_{SUB_ID}_{SKIP_DATE}",
        f"pause_sub_{SUB_ID}",
        f"edit_sub_{SUB_ID}",
        f"manage_items_{SUB_ID}",
        f"subhist_{SUB_ID}_1",
        f"cancel_sub_{SUB_ID}",
        "back_to_subscriptions",
    ]
    # Skip, Pause and Cancel wear the labels the backend published; Edit, Items, Orders and Back are the bot's own.
    assert labels(bot) == [
        LABELS["skip"], LABELS["pause"], "EDIT", "ITEMS", "ORDERS", LABELS["cancel"], "Back",
    ]


async def test_a_paused_subscription_offers_resume_not_pause_or_skip(bot, user):
    serve(bot, status="paused", actions=PAUSED)

    await bot.send(user.tap(f"subscription_{SUB_ID}"))

    assert buttons(bot) == [
        f"resume_sub_{SUB_ID}",
        f"edit_sub_{SUB_ID}",
        f"manage_items_{SUB_ID}",
        f"subhist_{SUB_ID}_1",
        f"cancel_sub_{SUB_ID}",
        "back_to_subscriptions",
    ]
    assert labels(bot)[0] == LABELS["resume"]


async def test_a_cancelled_subscription_offers_no_action_at_all(bot, user):
    """The bot used to offer Edit, Items and Cancel on a cancelled subscription."""
    serve(bot, status="cancelled", actions=CANCELLED)

    await bot.send(user.tap(f"subscription_{SUB_ID}"))

    assert buttons(bot) == [f"subhist_{SUB_ID}_1", "back_to_subscriptions"]


async def test_a_payload_without_allowed_actions_offers_nothing_the_bot_would_have_to_guess(bot, user):
    serve(bot, actions=None)

    await bot.send(user.tap(f"subscription_{SUB_ID}"))

    assert buttons(bot) == [f"subhist_{SUB_ID}_1", "back_to_subscriptions"]


async def test_the_items_screen_hides_what_the_backend_refuses(bot, user):
    """Edit allowed, remove_item refused (it is the last item): change and add stay, the bin goes."""
    serve(bot, actions=allowed(edit=None))

    await bot.send(user.tap(f"manage_items_{SUB_ID}"))

    assert buttons(bot) == [f"update_item_{SUB_ID}_{ITEM_ID}", f"add_item_{SUB_ID}", f"subscription_{SUB_ID}"]


async def test_the_items_screen_offers_the_bin_when_remove_item_is_allowed(bot, user):
    await bot.send(user.tap(f"manage_items_{SUB_ID}"))

    assert buttons(bot) == [
        f"update_item_{SUB_ID}_{ITEM_ID}", f"remove_item_{SUB_ID}_{ITEM_ID}",
        f"add_item_{SUB_ID}", f"subscription_{SUB_ID}",
    ]


async def test_the_items_screen_of_a_cancelled_subscription_offers_no_edit(bot, user):
    serve(bot, status="cancelled", actions=CANCELLED)

    await bot.send(user.tap(f"manage_items_{SUB_ID}"))

    assert buttons(bot) == [f"subscription_{SUB_ID}"]


# ---------------------------------------------------------------------------
# Cancel asks first
# ---------------------------------------------------------------------------


async def test_cancel_asks_before_it_cancels_and_sends_the_state_the_card_showed(bot, user):
    await bot.send(user.tap(f"cancel_sub_{SUB_ID}"))

    assert last_text(bot) == "\n".join([
        CANCEL_WORDS["confirm_text"], *CANCEL_WORDS["kept_lines"], *CANCEL_WORDS["cancel_lines"],
        CANCEL_WORDS["coins_line"],
    ])
    assert calls(bot, "POST", f"{SUBSCRIPTION}/cancel") == [], "the first tap cancelled without asking"
    assert buttons(bot) == [f"confirmcancel_sub_{SUB_ID}_{STATE}", f"subscription_{SUB_ID}"]
    assert labels(bot) == [CANCEL_WORDS["confirm_label"], CANCEL_WORDS["keep_label"]]

    await bot.send(user.tap(f"confirmcancel_sub_{SUB_ID}_{STATE}"))

    (cancel,) = calls(bot, "POST", f"{SUBSCRIPTION}/cancel")
    assert cancel.data == {"expected_state": STATE}
    assert "CANCELLED-OK" in answers(bot)


async def test_keeping_the_subscription_cancels_nothing(bot, user):
    await bot.send(user.tap(f"cancel_sub_{SUB_ID}"))
    bot.telegram.reset()

    await bot.send(user.tap(f"subscription_{SUB_ID}"))

    assert calls(bot, "POST", f"{SUBSCRIPTION}/cancel") == []
    assert last_text(bot).startswith("🔄 DETAILS")


async def test_the_cancel_confirmation_survives_a_restart(bot, user):
    """The id and the state ride the callback, so a deploy that empties user_data does not break the confirm."""
    await bot.send(user.tap(f"cancel_sub_{SUB_ID}"))
    bot.application.user_data[DEFAULT_USER_ID].clear()

    await bot.send(user.tap(f"confirmcancel_sub_{SUB_ID}_{STATE}"))

    assert len(calls(bot, "POST", f"{SUBSCRIPTION}/cancel")) == 1


# ---------------------------------------------------------------------------
# Skip names the date the customer saw
# ---------------------------------------------------------------------------


async def test_skip_asks_its_reason_then_sends_the_date_the_button_named(bot, user):
    from business_app.serializers.subscription_serializers import SkipDeliveryRequest

    bot.backend.route("POST", f"{SUBSCRIPTION}/skip-next-delivery", lambda _c: {"data": {}})
    await bot.send(user.tap(f"subscription_{SUB_ID}"))
    assert LABELS["skip"] in labels(bot)
    bot.telegram.reset()

    await bot.send(user.tap(f"skip_sub_{SUB_ID}_{SKIP_DATE}"))

    assert last_text(bot) == SKIP_WORDS["confirm_text"]
    assert calls(bot, "POST", f"{SUBSCRIPTION}/skip-next-delivery") == [], "the first tap skipped without asking"
    assert buttons(bot) == [
        f"skip_sub_{SUB_ID}_{SKIP_DATE}_away", f"skip_sub_{SUB_ID}_{SKIP_DATE}_other",
        f"skip_sub_{SUB_ID}_{SKIP_DATE}_none", f"subscription_{SUB_ID}",
    ]
    bot.telegram.reset()

    await bot.send(user.tap(f"skip_sub_{SUB_ID}_{SKIP_DATE}_none"))

    (skip,) = calls(bot, "POST", f"{SUBSCRIPTION}/skip-next-delivery")
    assert skip.data == {"expected_date": SKIP_DATE}
    SkipDeliveryRequest.model_validate(skip.data)  # the backend's own request model accepts it
    assert "SKIPPED-OK" in answers(bot)
    assert last_text(bot).startswith("🔄 DETAILS"), "the card was not refreshed after the skip"


async def test_a_skip_reason_is_sent_as_its_code(bot, user):
    bot.backend.route("POST", f"{SUBSCRIPTION}/skip-next-delivery", lambda _c: {"data": {}})

    await bot.send(user.tap(f"skip_sub_{SUB_ID}_{SKIP_DATE}_away"))

    (skip,) = calls(bot, "POST", f"{SUBSCRIPTION}/skip-next-delivery")
    assert skip.data == {"expected_date": SKIP_DATE, "reason_code": "away"}


async def test_a_409_this_changed_tells_the_customer_and_refreshes_the_card(bot, user):
    bot.backend.route(
        "POST", f"{SUBSCRIPTION}/skip-next-delivery",
        lambda _c: backend_failure("This changed. Refresh to see the latest.", 409),
    )

    await bot.send(user.tap(f"skip_sub_{SUB_ID}_{SKIP_DATE}_none"))

    assert alerts(bot) == ["This changed. Refresh to see the latest."]
    assert "SKIPPED-OK" not in answers(bot)
    assert last_text(bot).startswith("🔄 DETAILS"), "the stale card was left on screen"


async def test_a_skip_button_from_before_the_date_was_carried_skips_nothing(bot, user):
    """`skip_sub_12` (no date) is still tappable on an old card; the backend requires `expected_date`."""
    await bot.send(user.tap(f"skip_sub_{SUB_ID}"))

    assert calls(bot, "POST", f"{SUBSCRIPTION}/skip-next-delivery") == []
    assert alerts(bot) == ["CHANGED-REFRESH"]
    assert last_text(bot).startswith("🔄 DETAILS")


# ---------------------------------------------------------------------------
# Defects fixed on the way
# ---------------------------------------------------------------------------


async def test_removing_an_item_previews_then_saves_and_returns_to_that_subscriptions_items(bot, user):
    """The follow-up parsed the ITEM id as the subscription id (`.../subscriptions/33/items`); the removal is
    now previewed first and saved once, as the list without that item."""
    other = {"id": 34, "product_id": 8, "quantity": 1, "product": {"name": "Aqua 10L"}}
    sub = serve(bot, subscription_items=[
        {"id": ITEM_ID, "product_id": 7, "quantity": 2, "product": {"name": "Aqua 19L"}}, other,
    ])
    bot.backend.route("POST", f"{SUBSCRIPTION}/preview", lambda _c: {"data": {"preview": {"expected_state": STATE}}})
    bot.backend.route("PUT", f"{SUBSCRIPTION}/items", lambda _c: {"data": {"subscription": sub}})

    await bot.send(user.tap(f"remove_item_{SUB_ID}_{ITEM_ID}"))

    assert calls(bot, "DELETE", f"{SUBSCRIPTION}/items/{ITEM_ID}") == [], "the first tap removed without asking"
    assert calls(bot, "POST", f"{SUBSCRIPTION}/preview")[0].data == {"items": [{"product_id": 8, "quantity": 1}]}
    assert buttons(bot) == [f"subitems_del_{SUB_ID}_7_0_{STATE}", f"manage_items_{SUB_ID}"]

    await bot.send(user.tap(f"subitems_del_{SUB_ID}_7_0_{STATE}"))

    assert calls(bot, "PUT", f"{SUBSCRIPTION}/items")[0].data == {
        "items": [{"product_id": 8, "quantity": 1}], "expected_state": STATE,
    }
    assert [c.endpoint for c in bot.backend.calls if c.method == "GET" and c.endpoint.endswith("/items")] == []
    assert "ITEMS-UPDATED" in answers(bot)
    assert "CURRENT-ITEMS" in last_text(bot) and "Aqua 19L" in last_text(bot)
    assert f"update_item_{SUB_ID}_{ITEM_ID}" in buttons(bot)
    assert f"subscription_{SUB_ID}" in buttons(bot)


async def test_the_frequency_screens_back_returns_to_the_subscription(bot, user):
    await bot.send(user.tap(f"change_frequency_{SUB_ID}"))

    assert buttons(bot)[-1] == f"subscription_{SUB_ID}"
    await bot.send(user.tap(buttons(bot)[-1]))
    assert last_text(bot).startswith("🔄 DETAILS")


async def test_the_change_payment_menu_is_asked_with_the_subscriptions_address(bot, user):
    await bot.send(user.tap(f"change_payment_{SUB_ID}"))

    (asked,) = calls(bot, "GET", PAYMENT_METHODS)
    assert asked.params == {"context": "subscription", "delivery_address_id": ADDRESS_ID}
    assert last_text(bot) == "PICK-NEW-PAYMENT"


# ---------------------------------------------------------------------------
# Money: the backend's one price rule (spec 2026-10-26 §5 B10, §12), never the bot's own sum
# ---------------------------------------------------------------------------

NBSP = "\u00a0"


def serve_priced(bot, items, **extra):
    """A subscription whose stored figures are stale (``billing_amount``, the items' stored ``unit_price``)
    next to the labels the backend publishes at the current customer price, so a screen that reads the old
    fields shows a number nobody will be charged."""
    serve(bot, billing_amount=99999.0, subscription_items=items, **extra)


PRICED_ITEM = {
    "id": ITEM_ID, "product_id": 7, "quantity": 2, "unit_price": 12000.0, "total_price": 24000.0,
    "product": {"name": "Aqua 19L (old name)"}, "product_name": "Aqua 19L", "available": True,
    "min_order_quantity": 1, "unit_price_label": f"15{NBSP}000{NBSP}so'm", "line_total_label": f"30{NBSP}000{NBSP}so'm",
}
# What `serialize_subscription_item` publishes for a product that no longer exists.
DELETED_ITEM = {
    "id": 34, "product_id": 8, "quantity": 1, "unit_price": 5000.0, "total_price": 5000.0,
    "product": None, "product_name": None, "available": False, "min_order_quantity": 1,
    "unit_price_label": None, "line_total_label": None,
}


async def test_the_card_shows_the_published_per_delivery_estimate_not_the_stored_amount(bot, user):
    serve_priced(bot, [PRICED_ITEM], estimated_per_delivery=30000.0,
                 estimated_per_delivery_label=f"30{NBSP}000{NBSP}so'm")

    await bot.send(user.tap(f"subscription_{SUB_ID}"))

    assert last_text(bot) == (
        "🔄 DETAILS\n\n"
        "✅ Status: active\n"
        "📅 Every Thursday, 10:00–12:00\n"
        "\n📦 ITEMS-LABEL:\n"
        "  • Aqua 19L x2\n"
        f"\n💰 PER-DELIVERY: 30{NBSP}000{NBSP}so'm\n"
    )


async def test_the_items_screen_prices_each_line_at_the_published_label(bot, user):
    serve_priced(bot, [PRICED_ITEM])

    await bot.send(user.tap(f"manage_items_{SUB_ID}"))

    assert last_text(bot) == f"ITEMS\n\nCURRENT-ITEMS:\n  • Aqua 19L x2 - 30{NBSP}000{NBSP}so'm\n"
    assert labels(bot)[0] == "✏️ Aqua 19L x2"


async def test_a_deleted_product_shows_as_unknown_on_both_screens_instead_of_crashing(bot, user):
    """A deleted product is never delivered again (spec 2026-10-26 §8.5): named as unknown, marked not included."""
    serve_priced(bot, [PRICED_ITEM, DELETED_ITEM], estimated_per_delivery=30000.0,
                 estimated_per_delivery_label=f"30{NBSP}000{NBSP}so'm")

    await bot.send(user.tap(f"subscription_{SUB_ID}"))
    assert "  • UNKNOWN-PRODUCT x1 - NOT-INCLUDED\n" in last_text(bot)
    bot.telegram.reset()

    await bot.send(user.tap(f"manage_items_{SUB_ID}"))

    assert last_text(bot) == (
        f"ITEMS\n\nCURRENT-ITEMS:\n  • Aqua 19L x2 - 30{NBSP}000{NBSP}so'm\n  • UNKNOWN-PRODUCT x1 - NOT-INCLUDED\n"
    )
    assert labels(bot)[:3] == ["✏️ Aqua 19L x2", "🗑️", "✏️ UNKNOWN-PRODUCT x1"]


# What `serialize_subscription_item` publishes for a product that still exists but is no longer sold: its labels
# are still there (the current price), but `available: False` says it is left out of every order (§8.5).
DISCONTINUED_ITEM = {
    "id": 35, "product_id": 9, "quantity": 1, "unit_price": 8000.0, "total_price": 8000.0,
    "product": {"name": "Aqua 10L"}, "product_name": "Aqua 10L", "available": False, "min_order_quantity": 1,
    "unit_price_label": f"8{NBSP}000{NBSP}so'm", "line_total_label": f"8{NBSP}000{NBSP}so'm",
}


async def test_a_product_that_can_no_longer_be_delivered_is_not_priced_and_is_said_to_be_left_out(bot, user):
    """Re-review G10 (spec 2026-10-26 §8.5): the estimate leaves an unavailable product out, so its line must not
    show a price as if it will come, and both screens say it is left out, from the backend's `available` and
    `items_availability` answers (the ones the website renders as "Not included")."""
    serve_priced(bot, [PRICED_ITEM, DISCONTINUED_ITEM], items_availability="some", estimated_per_delivery=30000.0,
                 estimated_per_delivery_label=f"30{NBSP}000{NBSP}so'm")

    await bot.send(user.tap(f"subscription_{SUB_ID}"))

    assert last_text(bot) == (
        "🔄 DETAILS\n\n"
        "✅ Status: active\n"
        "📅 Every Thursday, 10:00–12:00\n"
        "\n📦 ITEMS-LABEL:\n"
        "  • Aqua 19L x2\n"
        "  • Aqua 10L x1 - NOT-INCLUDED\n"
        f"\n💰 PER-DELIVERY: 30{NBSP}000{NBSP}so'm\n"
        "\n⚠️ SOME-LEFT-OUT\n"
    )
    bot.telegram.reset()

    await bot.send(user.tap(f"manage_items_{SUB_ID}"))

    assert last_text(bot) == (
        f"ITEMS\n\nCURRENT-ITEMS:\n  • Aqua 19L x2 - 30{NBSP}000{NBSP}so'm\n  • Aqua 10L x1 - NOT-INCLUDED\n"
        "\n⚠️ SOME-LEFT-OUT\n"
    )
    assert f"8{NBSP}000" not in last_text(bot)


async def test_when_nothing_can_be_delivered_the_card_says_why_its_estimate_is_zero(bot, user):
    serve_priced(bot, [DISCONTINUED_ITEM], items_availability="none", estimated_per_delivery=0.0,
                 estimated_per_delivery_label=f"0{NBSP}so'm")

    await bot.send(user.tap(f"subscription_{SUB_ID}"))

    assert last_text(bot).endswith(
        "  • Aqua 10L x1 - NOT-INCLUDED\n"
        f"\n💰 PER-DELIVERY: 0{NBSP}so'm\n"
        "\n⚠️ NONE-LEFT\n"
    )


# ---------------------------------------------------------------------------
# The product picker: the subscription catalog at its published price (re-review G6, spec B10)
# ---------------------------------------------------------------------------

CATALOG = "/api/v1/subscriptions/products"
# What `GET /api/v1/subscriptions/products` publishes (`SubscriptionService.product_catalog`): a product on sale
# first (base 8 000, discount 6 000: its subscription price is 6 000), then more than ten others.
CATALOG_ROWS = [
    {"id": 7, "product_name": "Cooler cups", "images": [], "min_order_quantity": 2,
     "unit_price_label": f"6{NBSP}000{NBSP}so'm"},
] + [
    {"id": 100 + n, "product_name": f"Water {n:02d}", "images": [], "min_order_quantity": 1,
     "unit_price_label": f"{10 + n}{NBSP}000{NBSP}so'm"}
    for n in range(12)
]
PICKER_LINES = "".join(
    f"{index}. {row['product_name']} - {row['unit_price_label']}\n" for index, row in enumerate(CATALOG_ROWS, 1)
)


def serve_catalog(bot):
    bot.backend.route("GET", CATALOG, lambda _c: {"data": {"items": CATALOG_ROWS}})
    # The products API's checkout card would say 8000.0: the picker must never read it.
    bot.backend.route("GET", "/api/v1/products", lambda _c: {"data": {"items": [
        {"id": 7, "name": "Cooler cups", "pricing": {"base_price": 8000.0}},
    ]}})


async def test_the_creation_picker_lists_every_product_at_its_subscription_price(bot, user):
    serve_catalog(bot)

    await bot.send(user.tap("create_subscription"))

    assert last_text(bot) == f"PICK-A-PRODUCT\n\n{PICKER_LINES}"
    assert buttons(bot) == [f"sub_product_{row['id']}" for row in CATALOG_ROWS] + ["cancel_subscription_creation"]
    assert labels(bot)[0] == "➕ Cooler cups"
    assert calls(bot, "GET", "/api/v1/products") == []
    assert len(calls(bot, "GET", CATALOG)) == 1


async def test_the_add_item_picker_lists_every_product_it_can_still_take_at_its_subscription_price(bot, user):
    serve_catalog(bot)  # the subscription already holds product 7 (Cooler cups): changed from the list, not added twice
    rows = [row for row in CATALOG_ROWS if row["id"] != 7]
    lines = "".join(f"{index}. {row['product_name']} - {row['unit_price_label']}\n" for index, row in enumerate(rows, 1))

    await bot.send(user.tap(f"add_item_{SUB_ID}"))

    assert last_text(bot) == f"PICK-A-PRODUCT-TO-ADD\n\n{lines}"
    assert buttons(bot) == [f"sub_product_{row['id']}" for row in rows] + [f"manage_items_{SUB_ID}"]
    assert calls(bot, "GET", "/api/v1/products") == []


# ---------------------------------------------------------------------------
# Time slot change
# ---------------------------------------------------------------------------


async def test_the_edit_menu_offers_a_slot_change(bot, user):
    await bot.send(user.tap(f"edit_sub_{SUB_ID}"))

    assert f"change_slot_{SUB_ID}" in buttons(bot)
    assert "CHANGE-SLOT" in labels(bot)


async def test_changing_the_slot_offers_the_backend_filtered_slots_previews_then_saves_the_pick(bot, user):
    from business_app.serializers.subscription_serializers import (
        SubscriptionChangePreviewRequest,
        UpdateSubscriptionRequest,
    )

    bot.backend.route("POST", f"{SUBSCRIPTION}/preview", lambda _c: {"data": {"preview": {"expected_state": STATE}}})
    bot.backend.route("PUT", SUBSCRIPTION, lambda _c: {"data": {"subscription": {"id": SUB_ID}}})

    await bot.send(user.tap(f"change_slot_{SUB_ID}"))

    (asked,) = calls(bot, "GET", SLOTS)
    assert asked.params == {"weekday": 3, "frequency": "weekly"}  # Thursday, the subscription's own day
    assert last_text(bot) == "PICK-NEW-SLOT"
    assert buttons(bot) == [
        f"subsched_weekly_3_5_{SUB_ID}", f"subsched_weekly_3_6_{SUB_ID}", f"subsched_weekly_3_any_{SUB_ID}",
        f"subfreq_weekly_{SUB_ID}",
    ]
    bot.telegram.reset()

    await bot.send(user.tap(f"subsched_weekly_3_6_{SUB_ID}"))

    (previewed,) = calls(bot, "POST", f"{SUBSCRIPTION}/preview")
    assert previewed.data == {"frequency": "weekly", "delivery_day_of_week": 3, "delivery_time_slot_id": 6}
    SubscriptionChangePreviewRequest.model_validate(previewed.data)
    assert calls(bot, "PUT", SUBSCRIPTION) == [], "the preview saved"
    bot.telegram.reset()

    await bot.send(user.tap(f"subschedok_weekly_3_6_{SUB_ID}_{STATE}"))

    (saved,) = calls(bot, "PUT", SUBSCRIPTION)
    assert saved.data == {
        "frequency": "weekly", "delivery_day_of_week": 3, "delivery_time_slot_id": 6, "expected_state": STATE,
    }
    UpdateSubscriptionRequest.model_validate(saved.data)
    assert "SCHEDULE-UPDATED" in answers(bot)
    assert last_text(bot).startswith("🔄 DETAILS")


@pytest.mark.parametrize(
    "frequency, expected",
    [
        ("monthly", {"weekday": "any", "frequency": "monthly"}),
        ("daily", {"weekday": "any", "frequency": "daily"}),
        ("biweekly", {"weekday": 3, "frequency": "biweekly"}),
    ],
)
async def test_the_slot_question_carries_the_frequency_so_the_backend_applies_its_own_rule(
    bot, user, frequency, expected
):
    serve(bot, frequency=frequency)

    await bot.send(user.tap(f"change_slot_{SUB_ID}"))

    assert calls(bot, "GET", SLOTS)[-1].params == expected


async def test_no_offered_slot_offers_any_time_instead_of_a_dead_end(bot, user):
    bot.backend.route("GET", SLOTS, lambda _c: {"time_slots": []})

    await bot.send(user.tap(f"change_slot_{SUB_ID}"))

    assert last_text(bot) == "PICK-NEW-SLOT"
    assert labels(bot) == ["ANY-TIME", "Back"]
    assert buttons(bot) == [f"subsched_weekly_3_any_{SUB_ID}", f"subfreq_weekly_{SUB_ID}"]
    assert calls(bot, "PUT", SUBSCRIPTION) == []


async def test_the_slot_screen_still_works_after_a_restart(bot, user):
    bot.backend.route("PUT", SUBSCRIPTION, lambda _c: {"data": {"subscription": {"id": SUB_ID}}})
    await bot.send(user.tap(f"change_slot_{SUB_ID}"))
    bot.application.user_data[DEFAULT_USER_ID].clear()

    await bot.send(user.tap(f"subschedok_weekly_3_5_{SUB_ID}_{STATE}"))

    assert calls(bot, "PUT", SUBSCRIPTION)[0].data == {
        "frequency": "weekly", "delivery_day_of_week": 3, "delivery_time_slot_id": 5, "expected_state": STATE,
    }


async def test_an_old_slot_button_opens_the_picker_and_writes_nothing(bot, user):
    """`subslot_<slot>_<id>` named a slot but not the schedule it was for: it opens the picker again."""
    await bot.send(user.tap(f"subslot_5_{SUB_ID}"))

    assert last_text(bot) == "PICK-NEW-SLOT"
    assert f"subsched_weekly_3_5_{SUB_ID}" in buttons(bot)
    assert calls(bot, "PUT", SUBSCRIPTION) == [] and calls(bot, "POST", f"{SUBSCRIPTION}/preview") == []


# ---------------------------------------------------------------------------
# Statistics is gone
# ---------------------------------------------------------------------------


async def test_statistics_is_gone_from_the_list_and_from_the_dispatcher(bot, user):
    bot.backend.route("GET", "/api/v1/subscriptions", lambda _c: {"data": {"items": [
        {"id": SUB_ID, "status": "active", "name": "Haftalik", "frequency": "weekly"},
    ]}})

    await bot.send(user.tap("menu_subscriptions"))

    assert "subscription_statistics" not in buttons(bot)
    assert "STATS" not in labels(bot)
    assert bot.handlers_matching(user.tap("subscription_statistics")) == []
    assert not [c for c in bot.backend.calls if c.endpoint.endswith("/statistics")]


# ---------------------------------------------------------------------------
# Route compat: every body the bot sends passes the backend's own request model
# ---------------------------------------------------------------------------


async def test_the_bodies_the_bot_sends_pass_the_backends_request_models(bot, user):
    from business_app.serializers.subscription_serializers import (
        CancelSubscriptionRequest,
        ChangePaymentMethodRequest,
        PauseSubscriptionRequest,
        ReplaceSubscriptionItemsRequest,
        SkipDeliveryRequest,
        SubscriptionChangePreviewRequest,
        UpdateSubscriptionRequest,
    )

    for path in ("pause", "resume", "cancel", "skip-next-delivery", "change-payment-method", "preview"):
        bot.backend.route("POST", f"{SUBSCRIPTION}/{path}", lambda _c: {"data": {}})
    bot.backend.route("PUT", SUBSCRIPTION, lambda _c: {"data": {"subscription": {"id": SUB_ID}}})
    bot.backend.route("PUT", f"{SUBSCRIPTION}/items", lambda _c: {"data": {"subscription": {"id": SUB_ID}}})

    for tap in (
        f"confirmpause_sub_{SUB_ID}_{STATE}", f"resume_sub_{SUB_ID}", f"confirmcancel_sub_{SUB_ID}_{STATE}",
        f"skip_sub_{SUB_ID}_{SKIP_DATE}_away", f"skip_sub_{SUB_ID}_{SKIP_DATE}_none",
        f"subpayok_cash_{SUB_ID}_{STATE}", f"subsched_daily_x_any_{SUB_ID}", f"subschedok_daily_x_any_{SUB_ID}_{STATE}",
        f"subsched_weekly_3_5_{SUB_ID}", f"subschedok_weekly_3_5_{SUB_ID}_{STATE}",
        f"subitems_set_{SUB_ID}_7_3_{STATE}",
        f"subaddr_2_{SUB_ID}", f"subaddrpay_click_2_{SUB_ID}", f"subaddrok_2_click_{SUB_ID}_{STATE}",
        f"subaddrok_2_keep_{SUB_ID}_{STATE}",
    ):
        await bot.send(user.tap(tap))

    models = {
        "preview": SubscriptionChangePreviewRequest,
        "pause": PauseSubscriptionRequest,
        "cancel": CancelSubscriptionRequest,
        "skip-next-delivery": SkipDeliveryRequest,
        "change-payment-method": ChangePaymentMethodRequest,
    }
    checked = 0
    for call in bot.backend.calls:
        if call.method not in ("POST", "PUT") or not call.endpoint.startswith(SUBSCRIPTION):
            continue
        if call.method == "PUT":
            model = ReplaceSubscriptionItemsRequest if call.endpoint.endswith("/items") else UpdateSubscriptionRequest
        else:
            model = models.get(call.endpoint.rsplit("/", 1)[-1])
        if model is not None:
            model.model_validate(call.data or {})
            checked += 1
    assert checked >= 10, "the route-compat check saw too few subscription writes to mean anything"
