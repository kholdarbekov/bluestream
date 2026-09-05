"""Editing a subscription's ITEMS when the flow's subject has been forgotten.

WHY THIS FILE EXISTS
--------------------
The add-item and update-item flows keep the subscription they are editing in a
single ``user_data`` slot, ``editing_subscription_id``. Every step then reads it
with ``.get()`` — which never raises, and posts ``None`` instead:

* ``add_item_confirm`` calls ``add_subscription_item(token, None, item_data)``,
  i.e. ``POST /api/v1/subscriptions/None/items``
* ``add_item_back_to_products`` renders its product list with
  ``back_callback='manage_items_None'`` — a Back button guaranteed to land
  nowhere, minted fresh on a screen the customer is looking at right now

Separately, the "no addresses yet" screen inside subscription creation offers a
single button, and that button is ``add_address``. The only registered pattern
for adding an address is ``^add_new_address(_checkout)?$``. So the one escape
from that screen has never worked — no restart required, and no log line
anywhere, because an unclaimed callback query is invisible from the server side.
"""

import pytest

from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


SUB_ID = 12
PRODUCTS = "/api/v1/products"

TRANSLATIONS = {
    "telegram.subscription.flow_timed_out": "SUBSCRIPTION-SESSION-EXPIRED",
    "telegram.subscription.select_product_to_add": "PICK-A-PRODUCT-TO-ADD",
    "telegram.back": "Back",
}


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    harness.backend.route(
        "GET", PRODUCTS,
        lambda _c: {"data": {"items": [
            {"id": 7, "name": "Aqua Element 19L", "base_price": 25000},
        ]}},
    )
    harness.backend.route(
        "GET", f"/api/v1/subscriptions/{SUB_ID}",
        lambda _c: {"data": {"subscription": {
            "id": SUB_ID, "status": "active", "delivery_frequency": "weekly",
        }}},
    )
    harness.backend.route(
        "GET", f"/api/v1/subscriptions/{SUB_ID}/items",
        lambda _c: {"data": {"items": []}},
    )
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


def endpoints(bot):
    return [c.endpoint for c in bot.backend.calls]


def buttons(bot):
    markup = bot.telegram.last_shown().reply_markup or {}
    return [
        b.get("callback_data")
        for row in markup.get("inline_keyboard", [])
        for b in row
    ]


def acting_handlers(bot, update):
    return [
        (group, handler)
        for group, handler in bot.handlers_matching(update)
        if getattr(getattr(handler, "callback", None), "__name__", "")
        != "debug_callback_handler"
    ]


async def reach_the_add_item_quantity_screen(bot, user):
    await bot.send(user.tap(f"add_item_{SUB_ID}"))
    await bot.send(user.tap("sub_product_7"))


async def test_adding_an_item_never_posts_to_a_subscription_called_none(bot, user):
    """`/api/v1/subscriptions/None/items` is a URL the backend can only refuse,
    and the customer is shown its refusal as if their subscription were at
    fault."""
    await reach_the_add_item_quantity_screen(bot, user)
    bot.application.user_data[DEFAULT_USER_ID].pop("editing_subscription_id", None)
    bot.telegram.reset()

    await bot.send(user.tap("sub_qty_2"))

    assert not [e for e in endpoints(bot) if "/None" in e], (
        f"the bot posted to a None subscription: {endpoints(bot)}"
    )


async def test_the_add_item_back_button_is_never_minted_dead(bot, user):
    """A Back button carrying `manage_items_None` is drawn onto a screen the
    customer is looking at, and cannot be claimed by anything."""
    await reach_the_add_item_quantity_screen(bot, user)
    bot.application.user_data[DEFAULT_USER_ID].pop("editing_subscription_id", None)
    bot.telegram.reset()

    await bot.send(user.tap("back_to_product_selection"))

    if bot.telegram.shown:
        assert "manage_items_None" not in buttons(bot), (
            f"a dead Back button was rendered: {buttons(bot)}"
        )


async def test_the_no_addresses_screen_offers_a_button_that_exists(bot, user):
    """The only escape from 'no addresses yet' inside subscription creation.

    It used to emit `add_address`, while the one registered pattern for adding
    an address is `^add_new_address(_checkout)?$`. So the button had never
    worked — no restart needed, and nothing logged, because an unclaimed
    callback query is invisible from the server side.

    Asserted two ways on purpose: the dead literal must be gone from the module
    that minted it (these keyboards are built INLINE, so
    `test_callback_contract_customer`'s keyboards.py sweep cannot see them), and
    the callback it was replaced with must actually reach a handler.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "telegram_bot" / "handlers" / "subscriptions.py"
    ).read_text(encoding="utf-8")
    assert "callback_data='add_address'" not in source, (
        "the dead `add_address` literal is back; no pattern claims it"
    )

    assert acting_handlers(bot, user.tap("add_new_address")), (
        "'add_new_address' reaches no handler either — the replacement is as "
        "dead as the button it replaced"
    )
