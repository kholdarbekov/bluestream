"""Editing an EXISTING subscription, walked through the REAL dispatcher.

WHY THIS FILE EXISTS
--------------------
``test_subscription_flow_journeys.py`` covers subscription *creation*. The
*edit* branch — reached from a subscription card's "Edit" button — was never
driven by any test, and every screen in it was broken in production:

* ``billing_history_<id>``, ``change_payment_<id>`` and ``retry_billing_<id>`` (since removed)
  all parsed their id with ``split('_')[3]`` on a THREE-segment callback, so
  every tap raised ``IndexError`` and answered with the generic error toast.
  100% failure, every customer, no restart involved.
* The two ``ConversationHandler``s that were supposed to claim the follow-up
  taps (``frequency_update_handler`` / ``payment_update_handler``,
  telegram_bot/bot.py) were CONSTRUCTED and never ``add_handler``ed, so
  ``subscription_freq_*`` and ``sub_payment_*`` reached nothing at all on the
  edit path. Changing a subscription's frequency or payment rail from the bot
  had never once worked.
* ``retry_failed_billing`` and ``skip_delivery`` finish by delegating to
  ``subscription_details``, which re-parsed ``query.data`` with a DIFFERENT
  index (``[1]``). Even with the ``[3]`` bug fixed, the refresh raised
  ``ValueError: invalid literal for int() with base 10: 'billing'``.

The last point is why these callbacks now share ONE id parser
(``_subscription_id``), the way ``handlers/orders.py`` already has exactly one
``_cancelling_order_id``. Three indices for one question is how ``[3]`` drifted
in unnoticed.

The edit screens also carry the subscription id on the callback itself
(``subfreq_<freq>_<id>`` / ``subpay_<type>_<id>``) rather than in
``context.user_data``. Two reasons, both tested below: the Application is built
with no ``persistence``, so a deploy empties ``user_data`` while the card stays
tappable forever; and ``^subscription_freq_`` / ``^sub_payment_`` already belong
to the CREATION conversation, so reusing them made the two flows fight.

Only the three harness seams are faked; the dispatcher, the conversation state
machine, the keyboards and the real api_client endpoint paths are production
code.
"""

import pytest

from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


SUB_ID = 12
OTHER_SUB_ID = 34
SUBSCRIPTION = f"/api/v1/subscriptions/{SUB_ID}"
PAYMENT_METHODS = "/api/v1/payments/methods"


TRANSLATIONS = {
    "telegram.subscription.select_new_frequency": "PICK-NEW-FREQUENCY",
    "telegram.subscription.select_new_payment_method": "PICK-NEW-PAYMENT",
    "telegram.subscription.frequency_updated_successfully": "FREQUENCY-UPDATED",
    "telegram.subscription.payment_method_updated_successfully": "PAYMENT-UPDATED",
    "telegram.subscription.billing_history": "BILLING-HISTORY",
    "telegram.subscription.no_billing_history": "no billing yet",
    "telegram.subscription.billing_retry_initiated": "RETRY-STARTED",
    "telegram.subscription.details_title": "SUBSCRIPTION-DETAILS",
    "telegram.subscription.delivery_skipped": "DELIVERY-SKIPPED",
    "telegram.subscription.frequency_daily": "Daily",
    "telegram.subscription.frequency_weekly": "Weekly",
    "telegram.subscription.frequency_biweekly": "Biweekly",
    "telegram.subscription.frequency_monthly": "Monthly",
    "telegram.back": "Back",
}


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)

    for sub_id in (SUB_ID, OTHER_SUB_ID):
        harness.backend.route(
            "GET",
            f"/api/v1/subscriptions/{sub_id}",
            lambda _c, sid=sub_id: {
                "data": {
                    "subscription": {
                        "id": sid,
                        "status": "active",
                        "frequency": "weekly",
                    }
                }
            },
        )
    harness.backend.route(
        "GET",
        "/api/v1/delivery/time-slots",
        lambda _c: {
            "delivery_weekdays": [0, 1, 2, 3, 4, 5, 6],
            "time_slots": [{"id": 5, "name": "Morning", "time_range": "10:00-12:00"}],
        },
    )
    for sub_id in (SUB_ID, OTHER_SUB_ID):
        harness.backend.route(
            "POST",
            f"/api/v1/subscriptions/{sub_id}/preview",
            lambda _c: {"data": {"preview": {"schedule_sentence": "Every day", "expected_state": "0123456789abcdef"}}},
        )
    harness.backend.route(
        "GET",
        PAYMENT_METHODS,
        lambda _c: {
            "data": {
                "available_methods": [
                    {"method": "cash", "is_active": True},
                    {"method": "business_account", "is_active": True},
                ]
            }
        },
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
    """Every callback_data on the screen the customer is looking at."""
    markup = bot.telegram.last_shown().reply_markup or {}
    return [
        button.get("callback_data")
        for row in markup.get("inline_keyboard", [])
        for button in row
    ]


def endpoints(bot, method=None):
    return [
        call.endpoint
        for call in bot.backend.calls
        if method is None or call.method == method
    ]


def payload_for(bot, method, endpoint):
    for call in bot.backend.calls:
        if call.method == method and call.endpoint == endpoint:
            return call.data
    raise AssertionError(
        f"{method} {endpoint} was never called; the bot called {endpoints(bot)}"
    )


def collect_errors(bot):
    """Escaped exceptions, which otherwise vanish into the global handler."""
    errors = []
    bot.application.add_error_handler(
        lambda _update, context: errors.append(context.error) or None
    )
    return errors


# ---------------------------------------------------------------------------
# The three callbacks that parsed their id one segment past the end
# ---------------------------------------------------------------------------


async def test_the_billing_button_is_gone_with_its_always_empty_history(bot, user):
    """The Billing screen read `/billing-history`, which was always empty: no subscription is ever "billed",
    each delivery is an order. The route is retired (spec 2026-10-26 §5 B6), and so are its button and
    callback; the subscription's orders are its `/history`."""
    await bot.send(user.tap(f"subscription_{SUB_ID}"))

    assert not [b for b in buttons(bot) if b and b.startswith("billing_history")], buttons(bot)
    assert bot.handlers_matching(user.tap(f"billing_history_{SUB_ID}")) == []
    assert f"{SUBSCRIPTION}/billing-history" not in endpoints(bot)


async def test_the_change_payment_button_opens_the_picker(bot, user):
    """`change_payment_12` — same off-by-one, so the payment picker for an
    existing subscription could never be opened at all."""
    errors = collect_errors(bot)

    await bot.send(user.tap(f"change_payment_{SUB_ID}"))

    assert not errors, f"the tap still raises: {errors}"
    assert last_text(bot) == "PICK-NEW-PAYMENT", (
        f"the payment picker never rendered: {texts(bot)}"
    )


# The retry-billing button is gone (plan 2026-10-24 Task 4): no order is ever "billed", the next one is
# simply created. tests/telegram_bot/test_subscription_single_frequency_journeys.py asserts no card offers
# it and that its callback is claimed by nothing.


# ---------------------------------------------------------------------------
# The delegation crash the index fix alone does not reach
# ---------------------------------------------------------------------------


async def test_skipping_a_delivery_then_refreshes_that_subscriptions_card(bot, user):
    """`skip_sub_12` parsed its own id correctly at `[2]`, then handed the same
    callback to `subscription_details`, whose `[1]` is the literal 'sub'."""
    errors = collect_errors(bot)

    await bot.send(user.tap(f"skip_sub_{SUB_ID}"))

    assert not errors, f"the refresh still raises: {errors}"
    assert SUBSCRIPTION in endpoints(bot, "GET"), (
        "the subscription card was never refreshed after the skip"
    )


# ---------------------------------------------------------------------------
# The two follow-up screens whose buttons reached no handler at all
# ---------------------------------------------------------------------------


async def test_every_button_on_the_change_frequency_screen_is_claimed(bot, user):
    """The screen rendered four frequency buttons that matched no handler in any
    group: spinner, then nothing, forever."""
    await bot.send(user.tap(f"change_frequency_{SUB_ID}"))
    assert last_text(bot) == "PICK-NEW-FREQUENCY", f"wrong screen: {texts(bot)}"

    for callback in buttons(bot):
        assert bot.handlers_matching(user.tap(callback)), (
            f"{callback!r} on the change-frequency screen reaches no handler"
        )


async def test_choosing_a_new_frequency_asks_the_slot_and_writes_nothing_yet(bot, user):
    """A frequency tap is a step, not a save: the slot is asked next (spec 2026-10-27 §6.2), and the change is
    written only after the backend's preview has been shown and Saved."""
    await bot.send(user.tap(f"change_frequency_{SUB_ID}"))
    frequency_buttons = [b for b in buttons(bot) if b and b.startswith("subfreq_")]
    assert frequency_buttons, f"the screen offers no frequency buttons: {buttons(bot)}"
    bot.telegram.reset()

    await bot.send(user.tap(f"subfreq_daily_{SUB_ID}"))

    assert "PUT" not in [call.method for call in bot.backend.calls]
    assert buttons(bot) == [
        f"subsched_daily_x_5_{SUB_ID}", f"subsched_daily_x_any_{SUB_ID}", f"change_frequency_{SUB_ID}",
    ]


async def test_every_button_on_the_change_payment_screen_is_claimed(bot, user):
    await bot.send(user.tap(f"change_payment_{SUB_ID}"))
    assert last_text(bot) == "PICK-NEW-PAYMENT", f"wrong screen: {texts(bot)}"

    for callback in buttons(bot):
        assert bot.handlers_matching(user.tap(callback)), (
            f"{callback!r} on the change-payment screen reaches no handler"
        )


STATE = "0123456789abcdef"  # the card's `allowed_actions.expected_state`: 16 hex characters


def _editable_card(bot, note=None):
    """The card the consequence screen re-reads: editable, with the state a save must carry."""
    bot.backend.route(
        "GET", SUBSCRIPTION,
        lambda _c: {"data": {"subscription": {
            "id": SUB_ID, "status": "active", "frequency": "weekly", "kept_order_note": note,
            "allowed_actions": {"edit": {"allowed": True}, "expected_state": STATE},
        }}},
    )


async def test_choosing_a_new_payment_method_shows_the_consequence_then_updates_that_subscription(bot, user):
    _editable_card(bot, note="KEPT-NOTE")
    await bot.send(user.tap(f"change_payment_{SUB_ID}"))
    bot.telegram.reset()

    await bot.send(user.tap(f"subpay_cash_{SUB_ID}"))

    assert "POST" not in [call.method for call in bot.backend.calls], "the choice wrote at once"
    assert "KEPT-NOTE" in last_text(bot)
    assert buttons(bot) == [f"subpayok_cash_{SUB_ID}_{STATE}", f"change_payment_{SUB_ID}"]
    bot.telegram.reset()

    await bot.send(user.tap(f"subpayok_cash_{SUB_ID}_{STATE}"))

    assert payload_for(bot, "POST", f"{SUBSCRIPTION}/change-payment-method") == {
        "payment_method": "cash", "expected_state": STATE,
    }
    assert "PAYMENT-UPDATED" in [a.params.get("text") for a in bot.telegram.visible_answers]
    assert "SUBSCRIPTION-DETAILS" in last_text(bot)  # the card as it stands now


async def test_a_payment_method_whose_name_contains_an_underscore_survives(bot, user):
    """`business_account` is why the old parser used `split('_', 2)[2]`. The id
    now travels last, so the rail is everything between the prefix and the id."""
    _editable_card(bot)
    await bot.send(user.tap(f"change_payment_{SUB_ID}"))
    bot.telegram.reset()

    await bot.send(user.tap(f"subpay_business_account_{SUB_ID}"))
    assert buttons(bot)[0] == f"subpayok_business_account_{SUB_ID}_{STATE}"
    bot.telegram.reset()

    await bot.send(user.tap(f"subpayok_business_account_{SUB_ID}_{STATE}"))

    assert payload_for(bot, "POST", f"{SUBSCRIPTION}/change-payment-method") == {
        "payment_method": "business_account", "expected_state": STATE,
    }


# ---------------------------------------------------------------------------
# The two reasons the id rides the callback instead of user_data
# ---------------------------------------------------------------------------


async def test_the_edit_screens_still_work_after_a_restart(bot, user):
    """A deploy empties `user_data` while the card stays tappable. The id must
    come off the callback, or the preview posts to `subscriptions/None`."""
    await bot.send(user.tap(f"change_frequency_{SUB_ID}"))
    bot.application.user_data[DEFAULT_USER_ID].clear()  # the deploy
    bot.telegram.reset()

    await bot.send(user.tap(f"subfreq_daily_{SUB_ID}"))
    await bot.send(user.tap(f"subsched_daily_x_any_{SUB_ID}"))

    assert payload_for(bot, "POST", f"{SUBSCRIPTION}/preview") == {"frequency": "daily", "delivery_time_slot_id": None}
    assert "/api/v1/subscriptions/None" not in endpoints(bot)
    assert "/api/v1/subscriptions/None/preview" not in endpoints(bot)


async def test_editing_one_subscription_never_touches_another(bot, user):
    """`editing_subscription_id` was a single slot in user_data. Two cards open
    meant the second tap edited whichever subscription was stored last."""
    await bot.send(user.tap(f"change_frequency_{SUB_ID}"))
    await bot.send(user.tap(f"change_frequency_{OTHER_SUB_ID}"))
    bot.telegram.reset()

    await bot.send(user.tap(f"subfreq_daily_{SUB_ID}"))
    await bot.send(user.tap(f"subsched_daily_x_any_{SUB_ID}"))

    assert payload_for(bot, "POST", f"{SUBSCRIPTION}/preview") == {"frequency": "daily", "delivery_time_slot_id": None}
    assert f"/api/v1/subscriptions/{OTHER_SUB_ID}/preview" not in endpoints(bot, "POST"), (
        "the tap edited the other subscription's card"
    )


async def test_the_edit_screens_do_not_hijack_subscription_creation(bot, user):
    """`^subscription_freq_` and `^sub_payment_` belong to the CREATION
    conversation. The edit path must not compete for them."""
    creation_only = ["subscription_freq_weekly", "sub_payment_cash"]
    for callback in creation_only:
        claimed = bot.handlers_matching(user.tap(callback))
        for _group, handler in claimed:
            assert handler.callback.__name__ not in {
                "update_frequency_confirm",
                "change_payment_method_confirm",
            }, f"{callback!r} is claimed by the edit flow as well as creation"


# ---------------------------------------------------------------------------
# Pause carries the state the card showed: the endpoint is body-validated, so no body is a 400
# ---------------------------------------------------------------------------


async def test_pausing_sends_the_state_the_card_showed_not_an_empty_body(bot, user):
    """A pause that cancels nothing (`needs_confirm` False) goes out at once, with the `expected_state` of the
    card it just read: the backend answers 409 when that no longer holds."""
    state = "0123456789abcdef"
    bot.backend.route(
        "GET", SUBSCRIPTION,
        lambda _c: {"data": {"subscription": {
            "id": SUB_ID, "status": "active", "frequency": "weekly",
            "allowed_actions": {
                "pause": {"allowed": True, "label": "Pause", "needs_confirm": False},
                "expected_state": state,
            },
        }}},
    )
    bot.backend.route("POST", f"{SUBSCRIPTION}/pause", lambda _c: {"success": True, "data": {}})

    await bot.send(user.tap(f"pause_sub_{SUB_ID}"))

    assert payload_for(bot, "POST", f"{SUBSCRIPTION}/pause") == {"expected_state": state}
