"""The subscription flows, walked through the REAL dispatcher.

WHY THIS FILE EXISTS
--------------------
Subscription creation is the longest inline-only journey in the customer bot:
seven states, three of which render a keyboard whose Back button had no handler
at all. ``tests/telegram_bot/test_callback_contract_customer.py`` proves those
buttons are now CLAIMED, and by whom. Claimed is not the same as working — the
`back_to_product_selection` incident was a button claimed by a handler that
crashed on it — so this file taps them the way a customer does and asserts the
screen they land on and the state they end up in.

The same three flows also expire: `subscription_creation` after 10 minutes,
`item_management` and `update_item` after 5. Until 2026-08-22 they expired in
total silence, leaving `subscription_creation` / `editing_subscription_id` in
`user_data` for whatever the customer did next.

Only the three harness seams are faked; everything between them — the
conversation state machine, the keyboards, the real api_client endpoint paths —
is production code.
"""

import pytest
from telegram.ext import ConversationHandler

from handlers.subscriptions import (
    CONFIRM_SUBSCRIPTION,
    ITEM_SELECT_PRODUCT,
    ITEM_SELECT_QUANTITY,
    SELECT_ADDRESS,
    SELECT_FREQUENCY,
    SELECT_PAYMENT,
    SELECT_QUANTITY,
)

from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, build_bot_harness
# One expression of each shared trick rather than a second copy: ageing the
# dedup lock table is what the wall clock does, and PTB fires a conversation
# timeout itself rather than through `process_update`.
from tests.telegram_bot.test_cart_and_quantity_journeys import expire_dedup_window
from tests.telegram_bot.test_registration_journey_dispatcher import (
    fire_conversation_timeout,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


PRODUCTS = "/api/v1/products"
ADDRESSES = "/api/v1/auth/addresses"
PAYMENT_METHODS = "/api/v1/payments/methods"
TEMPLATES = "/api/v1/subscriptions/templates"

SUB_ID = 12
PRODUCT_ID = 7
ADDRESS_ID = 91


# Distinct strings, so "which screen is the customer on" is answerable from the
# text alone rather than from a keyboard shape that several screens share.
TRANSLATIONS = {
    "telegram.subscription.select_products": "PICK-A-PRODUCT",
    "telegram.subscription.select_product_to_add": "PICK-A-PRODUCT-TO-ADD",
    "telegram.subscription.select_quantity": "HOW-MANY",
    "telegram.subscription.select_quantity_for_item": "HOW-MANY-TO-ADD",
    "telegram.subscription.select_new_quantity": "NEW-QUANTITY",
    "telegram.subscription.item_added": "item added",
    "telegram.subscription.total_items": "items so far",
    "telegram.subscription.add_more_or_continue": "add more or continue",
    "telegram.subscription.select_frequency": "HOW-OFTEN",
    "telegram.subscription.select_address": "WHERE-TO",
    "telegram.subscription.select_payment": "HOW-TO-PAY",
    "telegram.subscription.no_addresses": "no addresses yet",
    "telegram.subscription.flow_timed_out": "SUBSCRIPTION-SESSION-EXPIRED",
    "telegram.back": "Back",
    "telegram.cancel": "Cancel",
    "telegram.continue": "Continue",
    "telegram.main_menu": "MAIN-MENU",
}


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)

    harness.backend.route(
        "GET",
        PRODUCTS,
        lambda _c: {
            "data": {
                "items": [
                    {"id": PRODUCT_ID, "name": "Aqua Element 19L", "base_price": 25000},
                    {"id": 8, "name": "Aqua Element 10L", "base_price": 16000},
                ]
            }
        },
    )
    harness.backend.route("GET", TEMPLATES, lambda _c: {"data": {}})
    harness.backend.addresses[ADDRESS_ID] = {
        "id": ADDRESS_ID,
        "address_line1": "15, Chilonzor dahasi",
        "city": "Toshkent",
        "is_default": True,
    }
    harness.backend.route(
        "GET",
        PAYMENT_METHODS,
        lambda _c: {
            "data": {
                "available_methods": [
                    {"method": "cash", "is_active": True},
                    {"method": "click", "is_active": True},
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


async def reach_quantity_screen(bot, user):
    """Creation flow, parked on the quantity keyboard."""
    await bot.send(user.tap("create_subscription"))
    assert bot.conversation_state("subscription_creation") == SELECT_QUANTITY
    await bot.send(user.tap(f"sub_product_{PRODUCT_ID}"))
    assert bot.conversation_state("subscription_creation") == SELECT_FREQUENCY
    assert last_text(bot) == "HOW-MANY"


async def reach_payment_screen(bot, user):
    """Creation flow, parked on the payment-method keyboard."""
    await reach_quantity_screen(bot, user)
    await bot.send(user.tap("sub_qty_2"))
    await bot.send(user.tap("sub_items_done"))
    assert bot.conversation_state("subscription_creation") == SELECT_ADDRESS
    await bot.send(user.tap("subscription_freq_weekly"))
    assert bot.conversation_state("subscription_creation") == SELECT_PAYMENT
    await bot.send(user.tap(f"addr_{ADDRESS_ID}"))
    assert bot.conversation_state("subscription_creation") == CONFIRM_SUBSCRIPTION
    assert last_text(bot) == "HOW-TO-PAY"


# ---------------------------------------------------------------------------
# The two Back buttons that landed nowhere
# ---------------------------------------------------------------------------


async def test_back_from_the_payment_screen_returns_to_the_address_list(bot, user):
    """The dangerous one: a customer part way through subscription checkout
    tapped Back and stayed on the payment screen, because
    `back_to_address_selection` matched no registered pattern anywhere.

    Back must re-show the address step and rewind the state to it, keeping the
    frequency they already chose.
    """
    await reach_payment_screen(bot, user)
    assert "back_to_address_selection" in buttons(bot), (
        "the payment screen stopped rendering the Back button this test is about"
    )
    bot.telegram.reset()

    await bot.send(user.tap("back_to_address_selection"))

    assert last_text(bot) == "WHERE-TO", (
        f"Back left the customer somewhere else: {texts(bot)}"
    )
    assert f"addr_{ADDRESS_ID}" in buttons(bot), "the address list came back empty"
    assert bot.conversation_state("subscription_creation") == SELECT_PAYMENT, (
        "the state must rewind with the screen, or the next tap is interpreted "
        "by the step the customer just left"
    )
    creation = bot.application.user_data[DEFAULT_USER_ID]["subscription_creation"]
    assert creation["delivery_frequency"] == "weekly", (
        "going back one step must not discard the frequency two steps back"
    )


async def test_back_from_the_quantity_screen_returns_to_the_product_list(bot, user):
    """`back_to_product_selection` used to fall through to the group-0
    `^back_to_product_` handler — `ProductHandlers.product_details` — which ran
    `int('selection')` and swallowed the ValueError. The customer saw nothing.
    """
    await reach_quantity_screen(bot, user)
    assert "back_to_product_selection" in buttons(bot)
    bot.telegram.reset()

    await bot.send(user.tap("back_to_product_selection"))

    assert last_text(bot).startswith("PICK-A-PRODUCT"), (
        f"Back did not re-show the product list: {texts(bot)}"
    )
    assert f"sub_product_{PRODUCT_ID}" in buttons(bot)
    assert bot.conversation_state("subscription_creation") == SELECT_QUANTITY


async def test_the_product_handler_never_sees_the_subscription_back_button(bot, user):
    """PTB walks EVERY group, so a conversation claiming the button in group -2
    does not stop the group-0 handler from running as well.

    The narrowed `^back_to_product_\\d+$` pattern is what actually keeps
    `ProductHandlers.product_details` away from it — asserted here through real
    dispatch, with an error handler attached because the harness has none.
    """
    errors = []
    bot.application.add_error_handler(
        lambda update, context: errors.append(context.error) or None
    )

    await reach_quantity_screen(bot, user)
    await bot.send(user.tap("back_to_product_selection"))

    assert not errors, f"an exception escaped to the dispatcher: {errors!r}"
    owners = {
        handler.callback.__qualname__
        for group, handler in bot.handlers_matching(user.tap("back_to_product_selection"))
        if group == 0
    }
    assert owners == set(), (
        f"a group-0 handler still claims the subscription Back button: {owners}"
    )


async def test_back_from_the_add_item_quantity_screen_returns_to_its_own_product_list(
    bot, user
):
    """The add-an-item flow renders the same quantity keyboard from a DIFFERENT
    product list — one whose own Back goes to the item-management menu. Its
    Back must return there, not to the subscription-creation product list.
    """
    await bot.send(user.tap(f"add_item_{SUB_ID}"))
    assert bot.conversation_state("item_management") == ITEM_SELECT_PRODUCT
    await bot.send(user.tap(f"sub_product_{PRODUCT_ID}"))
    assert bot.conversation_state("item_management") == ITEM_SELECT_QUANTITY
    assert last_text(bot) == "HOW-MANY-TO-ADD"
    assert "back_to_product_selection" in buttons(bot)
    bot.telegram.reset()

    await bot.send(user.tap("back_to_product_selection"))

    assert last_text(bot).startswith("PICK-A-PRODUCT-TO-ADD"), (
        f"Back left the add-item flow: {texts(bot)}"
    )
    assert f"manage_items_{SUB_ID}" in buttons(bot), (
        "the product list came back without its own way out"
    )
    assert bot.conversation_state("item_management") == ITEM_SELECT_PRODUCT


async def test_the_update_item_quantity_screen_offers_a_back_that_exists(bot, user):
    """The update flow never showed a product list, so
    `back_to_product_selection` would have offered a step that does not exist.

    It renders the item-management menu instead — which IS wired, at group 0.
    """
    await bot.send(user.tap(f"update_item_{SUB_ID}_33"))
    assert bot.conversation_state("update_item") == ITEM_SELECT_QUANTITY
    assert last_text(bot) == "NEW-QUANTITY"

    rendered = buttons(bot)
    assert "back_to_product_selection" not in rendered, (
        "the update flow offers a product step it never had"
    )
    assert f"manage_items_{SUB_ID}" in rendered

    assert bot.handlers_matching(user.tap(f"manage_items_{SUB_ID}")), (
        "the Back button this screen renders lands nowhere"
    )


# ---------------------------------------------------------------------------
# The three flows that used to expire in silence
# ---------------------------------------------------------------------------


async def test_a_subscription_being_built_says_so_when_it_expires(bot, user):
    """10 minutes of a customer comparing prices in another tab is normal.

    Before the TIMEOUT state existed, PTB dropped the conversation without a
    word and left `subscription_creation` — the half-built basket — in
    `user_data`, where the next flow would find it.
    """
    await reach_payment_screen(bot, user)
    flow_data = bot.application.user_data[DEFAULT_USER_ID]
    assert flow_data.get("subscription_creation"), "the half-built basket"
    bot.telegram.reset()

    result = await fire_conversation_timeout(
        bot, "subscription_creation", user.tap("addr_91")
    )

    assert result == ConversationHandler.END
    assert "SUBSCRIPTION-SESSION-EXPIRED" in texts(bot), (
        f"the customer was told nothing: {texts(bot)}"
    )
    assert "subscription_creation" not in flow_data, (
        "the half-built basket outlived the flow that owned it"
    )


@pytest.mark.parametrize(
    "flow,enter,keys",
    [
        ("item_management", f"add_item_{SUB_ID}", ("editing_subscription_id",)),
        (
            "update_item",
            f"update_item_{SUB_ID}_33",
            ("editing_subscription_id", "editing_item_id"),
        ),
    ],
)
async def test_the_item_flows_say_so_when_they_expire(bot, user, flow, enter, keys):
    """Both item flows write `editing_subscription_id`, and both used to leave
    it behind. A stale one makes the NEXT item edit target the wrong
    subscription — silently, because nothing re-reads it from the callback.
    """
    await bot.send(user.tap(enter))
    flow_data = bot.application.user_data[DEFAULT_USER_ID]
    for key in keys:
        assert key in flow_data, f"{flow} did not set {key}"
    bot.telegram.reset()

    result = await fire_conversation_timeout(bot, flow, user.tap(enter))

    assert result == ConversationHandler.END
    assert "SUBSCRIPTION-SESSION-EXPIRED" in texts(bot), (
        f"{flow} expired without telling the customer: {texts(bot)}"
    )
    for key in keys:
        assert key not in flow_data, f"{key} outlived {flow}"


# ---------------------------------------------------------------------------
# A cosmetic Telegram failure must not cost the step
# ---------------------------------------------------------------------------


async def test_a_refused_ack_does_not_end_the_subscription_flow(bot, user):
    """Telegram refuses `answerCallbackQuery` with "query is too old" whenever
    the tap sat in the update backlog — routine after every redeploy.

    Every handler here works inside one `try` that returns
    `ConversationHandler.END`, so a raised ack used to end the flow. The ack is
    cosmetic; the fetch and the render behind it are the reason the customer
    tapped.
    """
    await reach_quantity_screen(bot, user)
    bot.telegram.reset()
    bot.telegram.fail(
        "answerCallbackQuery",
        "Bad Request: query is too old and response timeout expired",
    )

    await bot.send(user.tap("sub_qty_2"))

    assert bot.conversation_state("subscription_creation") == SELECT_FREQUENCY, (
        "a spinner that could not be stopped ended the customer's subscription"
    )
    assert bot.application.user_data[DEFAULT_USER_ID]["subscription_creation"]["items"] == [
        {"product_id": PRODUCT_ID, "quantity": 2}
    ], "the work behind the ack did not happen"
    assert "sub_items_done" in buttons(bot), (
        "the customer was left with no way forward"
    )


async def test_a_message_telegram_refuses_to_edit_still_reaches_the_customer(bot, user):
    """The other half of the same class: `editMessageText` answers 400 "message
    to edit not found" once the bubble has been deleted, and "message is not
    modified" when the content really is unchanged.

    `BaseHandler._edit_or_replace_callback_message` treats the second as
    success and falls back to a fresh message for the first, so a RENDERING
    problem never becomes a FLOW problem.
    """
    await reach_quantity_screen(bot, user)
    bot.telegram.reset()
    bot.telegram.fail("editMessageText", "Bad Request: message to edit not found")

    await bot.send(user.tap("sub_qty_2"))

    assert [call.method for call in bot.telegram.shown].count("sendMessage") >= 1, (
        f"nothing replaced the message Telegram refused to edit: {bot.telegram.calls}"
    )
    assert bot.conversation_state("subscription_creation") == SELECT_FREQUENCY
