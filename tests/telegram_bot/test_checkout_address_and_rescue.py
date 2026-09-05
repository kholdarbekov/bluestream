"""Two checkout screens that keep going with a fact they no longer hold.

WHY THIS FILE EXISTS
--------------------
1. ``_show_payment_picker`` copies the chosen address's title and full text out
   of ``context.user_data['checkout_addresses']`` — a map built when the picker
   was first drawn. The ADDRESS ID rides the callback, so it survives; the map
   does not. With the map gone both fields become empty strings, and the
   confirmation screen then prints "Delivery address:" followed by nothing. The
   customer is asked to confirm an order without being shown where it is going.

2. ``select_payment_cash`` is the rescue button on the card shown when a card
   order dies on a Tax Committee (Asl belgisi) 503 — exactly the kind of outage
   during which a deploy is likely. It reads ``psp_failed_order_id`` to know
   WHICH order to rescue; with that gone it silently falls through to creating a
   brand new one, and ``confirm_order``'s guard then answers "missing
   information, please try again" — which says nothing about the order they
   were actually trying to rescue.
"""

import pytest

from tests.telegram_bot.test_checkout_journey_dispatcher import (  # noqa: F401
    add_address,
    bot,
    fill_cart,
    reach_payment_picker,
    shop,
    toasts,
    user,
    user_data,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


HOME = 501


def shown(bot):
    return [c.text for c in bot.telegram.shown if c.text]


async def test_the_picker_recovers_the_address_title_it_was_not_handed(
    bot, shop, user
):
    """The id survives on the callback; the map that names it does not."""
    add_address(bot, HOME, "Home", "15 Chilonzor", is_default=True)
    await fill_cart(bot, user)
    await bot.send(user.tap("cart_view"))
    await bot.send(user.tap("cart_checkout"))

    # The deploy: the address map goes, the card and its `address_<id>` stay.
    user_data(bot).pop("checkout_addresses", None)
    bot.telegram.reset()

    await bot.send(user.tap(f"address_{HOME}"))

    title = user_data(bot).get("selected_address_title")
    full = user_data(bot).get("selected_address_full")
    assert title or full, (
        "the picker carried an anonymous address forward: the confirmation "
        "screen will print 'Delivery address:' and then nothing"
    )


async def test_a_rescue_tap_without_its_order_says_so(bot, shop, user):
    """'Pay cash instead' for an order the bot can no longer name must not
    quietly become 'create a new order'."""
    await fill_cart(bot, user)
    bot.telegram.reset()

    await bot.send(user.tap("select_payment_cash"))

    said = [t for t in toasts(bot) if t] + shown(bot)
    assert said, (
        "the rescue button was tapped and the bot said nothing at all"
    )
    assert not any("Error occurred" in s for s in said), (
        f"the rescue degraded to a swallowed crash: {said}"
    )
