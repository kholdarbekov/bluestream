"""A brand-new customer signing up across a deploy.

WHY THIS FILE EXISTS
--------------------
Signup is the one journey where losing bot state costs a CUSTOMER, not just a
screen — and it had three independent breaks in it, all invisible because no
test could express "this person has no `users` row yet" until
``FakeDatabase(user=None)``.

The journey: ``/start`` renders the trilingual welcome card with
``set_language_uz|ru|en``. The customer picks a language, which is what actually
creates their account. Then they share their phone.

What a deploy in the middle used to do:

1. ``^set_language_`` is registered TWICE — inside the registration
   conversation's SELECT_LANGUAGE state (``profile.language_selection``, which
   calls ``register_telegram_user``) and standalone in group 0
   (``language.set_language``, for changing language later). Once the
   conversation state is gone the group-0 twin wins, and it never creates the
   row. ``update_user_language`` then matched zero rows, and the customer was
   shown a main menu for an account that does not exist. **Signup dead-ends at
   step one.**

2. ``filters.CONTACT`` is registered ONLY inside two conversation ``states``.
   The phone-request keyboard is a ``ReplyKeyboardMarkup`` — client-side state
   that survives any restart — so the contact arrives and matches nothing in any
   group. ``one_time_keyboard=True`` then hides the keyboard. No message, no
   error, no way forward.

3. The deep-link referral captured into ``user_data`` by ``_capture_referral_arg``
   is read many screens later at the registration POST. A deploy in between
   dropped it silently and unrecoverably — the customer's natural recovery is to
   re-send ``/start``, which arrives with no args.

Restarting is modelled the way it really happens: clear ``user_data`` AND the
conversation's own ``_conversations`` map, which is what dies with the process.
"""

import pytest

from tests.telegram_bot.ptb_harness import (
    DEFAULT_USER_ID,
    FakeDatabase,
    build_bot_harness,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


REGISTER = "/api/v1/auth/telegram-register"

TRANSLATIONS = {
    "telegram.registration.select_language": "PICK-A-LANGUAGE",
    "telegram.registration.enter_phone": "SHARE-YOUR-PHONE",
    "telegram.registration.phone_shared": "PHONE-SAVED",
    "telegram.language.already_selected": "already using this",
}


@pytest.fixture
async def bot(monkeypatch):
    """A customer with NO `users` row — the state /start begins in."""
    return await build_bot_harness(
        monkeypatch, translations=TRANSLATIONS, database=FakeDatabase(user=None)
    )


@pytest.fixture
def user(bot):
    return bot.updates()


def texts(bot):
    return [c.text for c in bot.telegram.shown if c.text]


def registrations(bot):
    return [c.data for c in bot.backend.calls if c.endpoint == REGISTER]


def restart(bot):
    """Everything the process actually loses on a deploy."""
    bot.application.user_data[DEFAULT_USER_ID].clear()
    for group in bot.application.handlers.values():
        for handler in group:
            conversations = getattr(handler, "_conversations", None)
            if conversations is not None:
                conversations.clear()
    bot.telegram.reset()


def language_buttons(bot):
    markup = bot.telegram.last_shown().reply_markup or {}
    return [
        b.get("callback_data")
        for row in markup.get("inline_keyboard", [])
        for b in row
        if str(b.get("callback_data", "")).startswith("set_language_")
    ]


async def test_the_language_tap_creates_the_account_even_after_a_restart(bot, user):
    """The tap that IS the signup. Falling through to the language-CHANGE twin
    left the customer with no `users` row and a main menu over the top of it."""
    await bot.send(user.command("start"))
    assert language_buttons(bot), f"the welcome card offered no language: {texts(bot)}"

    restart(bot)
    await bot.send(user.tap("set_language_ru"))

    assert registrations(bot), (
        "the language tap did not register the customer; the bot called "
        f"{[c.endpoint for c in bot.backend.calls]}"
    )
    assert registrations(bot)[0]["language_code"] == "ru"


async def test_the_customer_is_asked_for_their_phone_not_shown_a_menu(bot, user):
    """A main menu over a nonexistent account is worse than an error: every
    button on it fails, and nothing says why."""
    await bot.send(user.command("start"))
    restart(bot)

    await bot.send(user.tap("set_language_ru"))

    assert any("SHARE-YOUR-PHONE" in t for t in texts(bot)), (
        f"signup did not continue to the phone step: {texts(bot)}"
    )


async def test_sharing_the_phone_after_a_restart_is_not_ignored(bot, user):
    """`filters.CONTACT` lives only inside conversation states, but the
    request_contact keyboard is client-side and outlives any restart."""
    await bot.send(user.command("start"))
    await bot.send(user.tap("set_language_ru"))
    restart(bot)

    await bot.send(user.contact("+998901234567"))

    assert texts(bot), (
        "the shared contact matched no handler at all — the customer is left "
        "with no keyboard, no message and no way to finish signing up"
    )


async def test_a_referral_deep_link_survives_a_restart(bot, user):
    """`referral_code` lived only in `user_data` between /start and the
    registration POST, so a deploy in that window silently voided the
    referrer's reward and misattributed the acquisition channel."""
    await bot.send(user.command("start ref_ABC123"))
    # The button as the bot really rendered it, captured BEFORE the restart —
    # which is exactly where it lives in the real failure: on the customer's
    # phone, drawn by the process that is about to die.
    offered = language_buttons(bot)
    assert offered, "the welcome card offered no language buttons at all"
    uz = next(b for b in offered if b.startswith("set_language_uz"))

    restart(bot)
    await bot.send(user.tap(uz))

    assert registrations(bot), "the customer was never registered at all"
    assert registrations(bot)[0].get("referral_code") == "ABC123", (
        "the referral was dropped: the referrer is never credited and the "
        f"customer cannot recover it. Payload was {registrations(bot)[0]}"
    )


async def test_an_existing_customer_changing_language_is_not_re_registered(bot, user):
    """The guard must not turn the ordinary Profile -> Language tap into a
    signup. An existing customer changes language and stays themselves."""
    bot.database.user = {
        "id": 398,
        "telegram_id": str(DEFAULT_USER_ID),
        "first_name": "Kamola",
        "phone": "+998978730111",
        "preferred_language": "uz",
        "role": "customer",
        "status": "active",
        "bot_state": "{}",
        "user_type": "individual",
    }
    bot.telegram.reset()

    await bot.send(user.tap("set_language_ru"))

    assert not registrations(bot), (
        "an existing customer's language change was treated as a new signup"
    )
