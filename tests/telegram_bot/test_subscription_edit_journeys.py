"""Editing an EXISTING subscription, walked through the REAL dispatcher.

WHY THIS FILE EXISTS
--------------------
``test_subscription_flow_journeys.py`` covers subscription *creation*. The
*edit* branch — reached from a subscription card's "Edit" button — was never
driven by any test, and every screen in it was broken in production:

* ``billing_history_<id>``, ``change_payment_<id>`` and ``retry_billing_<id>``
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
                        "delivery_frequency": "weekly",
                    }
                }
            },
        )
        harness.backend.route(
            "GET",
            f"/api/v1/subscriptions/{sub_id}/items",
            lambda _c: {"data": {"items": []}},
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


async def test_the_billing_button_reaches_that_subscriptions_history(bot, user):
    """`billing_history_12`.split('_') is ['billing','history','12'] — index 3
    does not exist. Every tap raised IndexError and showed the error toast."""
    errors = collect_errors(bot)

    await bot.send(user.tap(f"billing_history_{SUB_ID}"))

    assert not errors, f"the tap still raises: {errors}"
    assert f"{SUBSCRIPTION}/billing-history" in endpoints(bot), (
        f"billing history was never requested; the bot called {endpoints(bot)}"
    )
    assert "BILLING-HISTORY" in last_text(bot)


async def test_the_change_payment_button_opens_the_picker(bot, user):
    """`change_payment_12` — same off-by-one, so the payment picker for an
    existing subscription could never be opened at all."""
    errors = collect_errors(bot)

    await bot.send(user.tap(f"change_payment_{SUB_ID}"))

    assert not errors, f"the tap still raises: {errors}"
    assert last_text(bot) == "PICK-NEW-PAYMENT", (
        f"the payment picker never rendered: {texts(bot)}"
    )


async def test_the_retry_billing_button_reaches_the_backend(bot, user):
    """`retry_billing_12` — same off-by-one."""
    errors = collect_errors(bot)

    await bot.send(user.tap(f"retry_billing_{SUB_ID}"))

    assert not errors, f"the tap still raises: {errors}"
    assert f"{SUBSCRIPTION}/retry-billing" in endpoints(bot, "POST")


# ---------------------------------------------------------------------------
# The delegation crash the index fix alone does not reach
# ---------------------------------------------------------------------------


async def test_retrying_billing_then_refreshes_that_subscriptions_card(bot, user):
    """`retry_failed_billing` ends with `await self.subscription_details(...)`,
    which re-parsed the SAME callback with `split('_')[1]` -> `int('billing')`.

    Fixing only the `[3]` index leaves this ValueError in place, so the customer
    sees the retry toast and then the error toast.
    """
    errors = collect_errors(bot)

    await bot.send(user.tap(f"retry_billing_{SUB_ID}"))

    assert not errors, f"the refresh still raises: {errors}"
    assert SUBSCRIPTION in endpoints(bot, "GET"), (
        "the subscription card was never refreshed after the retry"
    )
    assert "SUBSCRIPTION-DETAILS" in last_text(bot)


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


async def test_choosing_a_new_frequency_updates_that_subscription(bot, user):
    await bot.send(user.tap(f"change_frequency_{SUB_ID}"))
    frequency_buttons = [b for b in buttons(bot) if b and b.startswith("subfreq_")]
    assert frequency_buttons, f"the screen offers no frequency buttons: {buttons(bot)}"
    bot.telegram.reset()

    await bot.send(user.tap(f"subfreq_monthly_{SUB_ID}"))

    assert payload_for(bot, "PUT", SUBSCRIPTION) == {"frequency": "monthly"}
    assert last_text(bot) == "✅ FREQUENCY-UPDATED"


async def test_every_button_on_the_change_payment_screen_is_claimed(bot, user):
    await bot.send(user.tap(f"change_payment_{SUB_ID}"))
    assert last_text(bot) == "PICK-NEW-PAYMENT", f"wrong screen: {texts(bot)}"

    for callback in buttons(bot):
        assert bot.handlers_matching(user.tap(callback)), (
            f"{callback!r} on the change-payment screen reaches no handler"
        )


async def test_choosing_a_new_payment_method_updates_that_subscription(bot, user):
    await bot.send(user.tap(f"change_payment_{SUB_ID}"))
    bot.telegram.reset()

    await bot.send(user.tap(f"subpay_cash_{SUB_ID}"))

    assert payload_for(bot, "POST", f"{SUBSCRIPTION}/change-payment-method") == {
        "payment_method": "cash"
    }
    assert last_text(bot) == "✅ PAYMENT-UPDATED"


async def test_a_payment_method_whose_name_contains_an_underscore_survives(bot, user):
    """`business_account` is why the old parser used `split('_', 2)[2]`. The id
    now travels last, so the rail is everything between the prefix and the id."""
    await bot.send(user.tap(f"change_payment_{SUB_ID}"))
    bot.telegram.reset()

    await bot.send(user.tap(f"subpay_business_account_{SUB_ID}"))

    assert payload_for(bot, "POST", f"{SUBSCRIPTION}/change-payment-method") == {
        "payment_method": "business_account"
    }


# ---------------------------------------------------------------------------
# The two reasons the id rides the callback instead of user_data
# ---------------------------------------------------------------------------


async def test_the_edit_screens_still_work_after_a_restart(bot, user):
    """A deploy empties `user_data` while the card stays tappable. The id must
    come off the callback, or the update posts to `subscriptions/None`."""
    await bot.send(user.tap(f"change_frequency_{SUB_ID}"))
    bot.application.user_data[DEFAULT_USER_ID].clear()  # the deploy
    bot.telegram.reset()

    await bot.send(user.tap(f"subfreq_daily_{SUB_ID}"))

    assert payload_for(bot, "PUT", SUBSCRIPTION) == {"frequency": "daily"}
    assert "/api/v1/subscriptions/None" not in endpoints(bot)


async def test_editing_one_subscription_never_touches_another(bot, user):
    """`editing_subscription_id` was a single slot in user_data. Two cards open
    meant the second tap edited whichever subscription was stored last."""
    await bot.send(user.tap(f"change_frequency_{SUB_ID}"))
    await bot.send(user.tap(f"change_frequency_{OTHER_SUB_ID}"))
    bot.telegram.reset()

    await bot.send(user.tap(f"subfreq_daily_{SUB_ID}"))

    assert payload_for(bot, "PUT", SUBSCRIPTION) == {"frequency": "daily"}
    assert f"/api/v1/subscriptions/{OTHER_SUB_ID}" not in endpoints(bot, "PUT"), (
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
