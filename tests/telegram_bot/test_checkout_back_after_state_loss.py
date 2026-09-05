"""The checkout Back buttons, tapped on a card that outlived the bot's memory.

WHY THIS FILE EXISTS
--------------------
This is the production crash of 2026-09-03 14:26:31, reproduced:

    Bot handler error in back_to_payment: 'selected_address_id'
    File "telegram_bot/handlers/orders.py", line 1763, in back_to_payment
        update, context, context.user_data['selected_address_id']
    KeyError: 'selected_address_id'

Six seconds earlier the bot had restarted. The Application is built with no
``persistence``, so ``context.user_data`` is process memory and every customer's
checkout selections went with it — while the confirmation card, like every
inline keyboard Telegram has ever delivered, stayed on the customer's phone with
live buttons. They tapped "⬅️ Back" and the handler read
``context.user_data['selected_address_id']`` as a bare subscript.

Not a queued update replayed on startup: ``drop_pending_updates`` defaults to
``true`` and is set in no ``.env`` or compose file, so the backlog is discarded.
A fresh tap on an old card is the whole mechanism.

The restart is not the only way in, which is why the fix carries the address on
the callback rather than merely guarding the read. ``confirm_order`` calls
``context.user_data.clear()`` and THEN edits the same message into the payment
card, whose Back button re-renders a confirmation screen — so a card/click
customer reaches a live ``back_to_payment`` button on an already-empty
``user_data`` without any deploy at all.

``confirm_order`` two screens away already reads the same key with ``.get()``
and answers an absent one with ``telegram.orders.missing_info``. Same file, same
flow, 130 lines apart.
"""

import pytest

from tests.telegram_bot.test_checkout_journey_dispatcher import (  # noqa: F401
    BOTTLE_19L,
    add_address,
    assert_no_swallowed_crash,
    bot,
    fill_cart,
    handlers_that_would_act,
    reach_confirmation,
    reach_payment_picker,
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
        button.get("callback_data")
        for row in markup.get("inline_keyboard", [])
        for button in row
    ]


def back_to_payment_button(bot):
    """The Back button as the confirmation card actually rendered it."""
    found = [b for b in buttons(bot) if b and b.startswith("back_to_payment")]
    assert found, f"the confirmation card offers no Back button: {buttons(bot)}"
    return found[0]


async def test_back_from_the_confirmation_card_survives_a_deploy(bot, shop, user):
    """The seed crash, start to finish."""
    add_address(bot, HOME, "Home", "15 Chilonzor", is_default=True)
    await fill_cart(bot, user)
    await reach_confirmation(bot, user, HOME)
    back = back_to_payment_button(bot)

    user_data(bot).clear()  # the 14:26:25 restart
    bot.telegram.reset()

    await bot.send(user.tap(back))

    assert_no_swallowed_crash(bot)
    assert "payment" in str(buttons(bot)), (
        f"Back did not reopen the payment picker; the customer got {buttons(bot)}"
    )


async def test_the_back_button_reopens_the_picker_for_the_right_address(bot, shop, user):
    """Carrying the address on the callback is what makes the screen rebuildable.

    With two saved addresses, a Back that has forgotten which one was chosen
    cannot re-price the rails: `/payments/methods` takes the address, and the
    COD cap and tier discount are derived per place.
    """
    add_address(bot, HOME, "Home", "15 Chilonzor", is_default=True)
    add_address(bot, 502, "Office", "1 Amir Temur")
    await fill_cart(bot, user)
    await reach_confirmation(bot, user, 502)
    back = back_to_payment_button(bot)

    user_data(bot).clear()
    bot.telegram.reset()

    await bot.send(user.tap(back))

    assert_no_swallowed_crash(bot)
    assert user_data(bot).get("selected_address_id") == 502, (
        "Back reopened the picker for a different address than the one the "
        f"customer had chosen: {user_data(bot).get('selected_address_id')}"
    )


async def test_the_back_button_is_claimed_by_a_handler(bot, shop, user):
    """A narrowed pattern that stops matching its own button is a dead button,
    and the generic error toast looks identical to a spinner from the outside."""
    add_address(bot, HOME, "Home", "15 Chilonzor", is_default=True)
    await fill_cart(bot, user)
    await reach_confirmation(bot, user, HOME)

    back = back_to_payment_button(bot)
    assert handlers_that_would_act(bot, user.tap(back)), (
        f"{back!r} reaches no handler that would act on it"
    )


async def test_back_without_an_address_says_so_instead_of_crashing(bot, shop, user):
    """A card minted by the release BEFORE this one carries the bare
    `back_to_payment`. It must be answered honestly, not with a traceback."""
    add_address(bot, HOME, "Home", "15 Chilonzor", is_default=True)
    await fill_cart(bot, user)
    await reach_confirmation(bot, user, HOME)

    user_data(bot).clear()
    bot.telegram.reset()

    await bot.send(user.tap("back_to_payment"))

    assert "Error occurred" not in toasts(bot), (
        f"the legacy card still produces a swallowed crash: {toasts(bot)}"
    )
    assert toasts(bot), (
        "the legacy Back button was not answered at all — the spinner runs to "
        "Telegram's client timeout and the customer is told nothing"
    )


async def test_back_still_works_normally_without_any_state_loss(bot, shop, user):
    """The guard must not cost the ordinary customer their Back button."""
    add_address(bot, HOME, "Home", "15 Chilonzor", is_default=True)
    await fill_cart(bot, user)
    await reach_confirmation(bot, user, HOME)
    back = back_to_payment_button(bot)
    bot.telegram.reset()

    await bot.send(user.tap(back))

    assert_no_swallowed_crash(bot)
    assert user_data(bot).get("selected_address_id") == HOME
    assert "confirm_order" not in buttons(bot), (
        "Back left the customer on the confirmation screen it was supposed to "
        "step away from"
    )
