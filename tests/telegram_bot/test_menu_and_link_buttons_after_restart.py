"""Buttons that answer nothing: the menu, the link prompt, and two dead literals.

WHY THIS FILE EXISTS
--------------------
Four separate ways a customer taps a live-looking button and the bot does not so
much as answer the callback query. Telegram spins it until the client gives up,
and nothing is logged, because an unanswered query is indistinguishable from a
tap that never happened.

1. ``menu.main_menu_handler`` edits the tapped message with
   ``edit_message_text``. A one-category shop sends its product list as a PHOTO
   (``products.py`` deletes the message and ``send_photo``s the list), and a
   photo has no text to edit — Telegram answers 400 "there is no text in the
   message to edit". The Back button on that screen therefore raises, and the
   ``except`` only logs. ``BaseHandler._edit_or_replace_callback_message``
   exists for exactly this and this call site does not use it.

2. ``loyalty.loyalty_menu`` returns the moment ``user_middleware`` gives back
   ``None``, having made ZERO Telegram calls. Every main-menu card ever sent
   stays tappable forever, so this is reachable long after the card was drawn.

3. ``link_account_confirm``'s ``link_yes`` / ``link_no`` are registered ONLY
   inside the registration conversation's LINK_ACCOUNT_CONFIRM state. The prompt
   appears at the most delicate moment in signup — the customer's phone already
   belongs to a web account — and after a deploy neither answer lands anywhere.

4. ``menu_main`` on the subscription-created success card is a literal no
   pattern claims; the registered main-menu callback is ``back_to_main``. Minted
   inline in ``handlers/subscriptions.py``, so the keyboards.py sweep in
   ``test_callback_contract_customer`` cannot see it either.
"""

import pytest

from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


TRANSLATIONS = {
    "telegram.main_menu": "MAIN-MENU",
    "telegram.registration.flow_timed_out": "SIGNUP-STEP-EXPIRED",
}


@pytest.fixture
async def bot(monkeypatch):
    return await build_bot_harness(monkeypatch, translations=TRANSLATIONS)


@pytest.fixture
def user(bot):
    return bot.updates()


def restart(bot):
    bot.application.user_data[DEFAULT_USER_ID].clear()
    for group in bot.application.handlers.values():
        for handler in group:
            conversations = getattr(handler, "_conversations", None)
            if conversations is not None:
                conversations.clear()
    bot.telegram.reset()


def acting_handlers(bot, update):
    return [
        (group, handler)
        for group, handler in bot.handlers_matching(update)
        if getattr(getattr(handler, "callback", None), "__name__", "")
        != "debug_callback_handler"
    ]


def answered(bot):
    return bot.telegram.of("answerCallbackQuery")


def shown(bot):
    return [c.text for c in bot.telegram.shown if c.text]


async def test_back_to_the_menu_works_from_a_photo_message(bot, user):
    """A one-category shop's product list is a PHOTO, and `edit_message_text`
    cannot edit one. The Back button on it must still reach the menu."""
    bot.telegram.fail(
        "editMessageText",
        "Bad Request: there is no text in the message to edit",
    )
    bot.telegram.reset()

    await bot.send(user.tap("back_to_main"))

    # The `except` in main_menu_handler answers with a generic error TOAST and
    # sends no message, so the menu never arrives: the customer is stranded on
    # a photo whose only way out just failed.
    toasts = [
        c.params.get("text") for c in bot.telegram.of("answerCallbackQuery")
        if c.params.get("text")
    ]
    assert not any("error" in t.lower() for t in toasts), (
        f"Back from a photo screen degraded to the generic error toast: {toasts}"
    )
    assert any("MAIN-MENU" in t for t in shown(bot)), (
        f"Back from a photo screen never reached the menu; sent {shown(bot)}, "
        f"toasts {toasts}"
    )


async def test_the_loyalty_menu_always_answers_the_tap(bot, user):
    """Returning without answering leaves the button spinning to the client
    timeout, with nothing logged and nothing on screen."""
    bot.database.user = None  # user_middleware's None branch
    bot.telegram.reset()

    await bot.send(user.tap("menu_loyalty"))

    assert answered(bot) or shown(bot), (
        "the Aqua Club button was tapped and the bot made no Telegram call at "
        "all — the customer watches a spinner and is told nothing"
    )


@pytest.mark.parametrize("callback", ["link_yes", "link_no"])
async def test_the_account_link_answers_are_claimed_after_a_restart(bot, user, callback):
    """The prompt appears at the most delicate moment in signup — the phone
    already belongs to a web account — and both answers were registered only
    inside a conversation state that a deploy erases."""
    restart(bot)

    assert acting_handlers(bot, user.tap(callback)), (
        f"{callback!r} reaches no handler once the conversation is gone"
    )


async def test_the_subscription_success_card_offers_a_menu_button_that_exists(bot, user):
    """`menu_main` is claimed by nothing; the registered callback is
    `back_to_main`. Minted inline, so the keyboards.py contract sweep is blind
    to it."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "telegram_bot" / "handlers" / "subscriptions.py"
    ).read_text(encoding="utf-8")
    assert "callback_data='menu_main'" not in source, (
        "the dead `menu_main` literal is back; no registered pattern claims it"
    )

    assert acting_handlers(bot, user.tap("back_to_main")), (
        "'back_to_main' reaches no handler either"
    )
