"""The confirmation screen, rendered when the checkout it describes is gone.

WHY THIS FILE EXISTS
--------------------
``_show_order_confirmation`` reads every fact it prints with ``.get()``, so it
never raises — it just draws a card with the facts missing:

* ``if address_id:`` is False, so the delivery-address line is omitted entirely
* ``if payment_method:`` is False, so the rail line is omitted too, and the
  payable figure is quoted with the tier discount silently dropped — a COD
  customer sees a HIGHER total than the picker just showed them, with no rail
  named to explain it
* the Confirm button is still live, and ``confirm_order`` guards the same two
  keys — so the only thing it can ever answer is "missing information, please
  try again", advice that cannot succeed on that card

Four top-level callbacks reach it (``payment_*``, ``back_to_order_confirm``,
``checkout_apply_reward_*``, ``checkout_remove_reward``), so any of them tapped
on a card that outlived its process lands here.

And it needs no deploy at all. ``confirm_order`` runs
``context.user_data.clear()`` and THEN edits the same message into the payment
card, whose Back is ``back_to_order_confirm`` — so a card/click customer reaches
this screen on an already-empty ``user_data`` by tapping Back, and the ghost
"Confirm your order" it draws is for an order that has already been placed.

The screen has to refuse instead of drawing a card it cannot honour.
``confirm_order`` two methods away already reads the same two keys with
``.get()`` and answers ``telegram.orders.missing_info`` when they are absent;
this is that guard, moved to the screen that draws the button.
"""

import pytest

from tests.telegram_bot.test_checkout_journey_dispatcher import (  # noqa: F401
    add_address,
    bot,
    fill_cart,
    reach_confirmation,
    shop,
    toasts,
    user,
    user_data,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


HOME = 501


def buttons(bot):
    markup = bot.telegram.last_shown().reply_markup or {}
    return [
        b.get("callback_data")
        for row in markup.get("inline_keyboard", [])
        for b in row
    ]


def shown(bot):
    return [c.text for c in bot.telegram.shown if c.text]


async def test_the_confirmation_screen_refuses_rather_than_drawing_a_blank_card(
    bot, shop, user
):
    """A card with no address, no rail and a live Confirm is worse than an
    error: every route out of it fails, and nothing on it says why."""
    add_address(bot, HOME, "Home", "15 Chilonzor", is_default=True)
    await fill_cart(bot, user)
    await reach_confirmation(bot, user, HOME)

    user_data(bot).clear()
    bot.telegram.reset()

    await bot.send(user.tap("back_to_order_confirm"))

    offered = buttons(bot) if bot.telegram.shown else []
    assert "confirm_order" not in offered, (
        "the screen still offers a Confirm button whose only possible answer is "
        f"'missing information'. It rendered: {shown(bot)}"
    )


async def test_the_customer_is_told_the_checkout_expired(bot, shop, user):
    """Refusing silently is the same dead end with fewer pixels."""
    add_address(bot, HOME, "Home", "15 Chilonzor", is_default=True)
    await fill_cart(bot, user)
    await reach_confirmation(bot, user, HOME)

    user_data(bot).clear()
    bot.telegram.reset()

    await bot.send(user.tap("back_to_order_confirm"))

    said = [t for t in toasts(bot) if t] + shown(bot)
    assert said, "the customer was shown and told absolutely nothing"
    assert not any("Error occurred" in s for s in said), (
        f"the refusal surfaced as a swallowed crash: {said}"
    )


async def test_an_intact_checkout_still_reaches_the_confirmation_screen(bot, shop, user):
    """The guard must fire only when the checkout is genuinely gone."""
    add_address(bot, HOME, "Home", "15 Chilonzor", is_default=True)
    await fill_cart(bot, user)
    await reach_confirmation(bot, user, HOME)
    bot.telegram.reset()

    await bot.send(user.tap("back_to_order_confirm"))

    assert "confirm_order" in buttons(bot), (
        f"a perfectly live checkout lost its Confirm button: {buttons(bot)}"
    )
