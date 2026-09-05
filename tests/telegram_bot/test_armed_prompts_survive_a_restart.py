"""Prompts the bot armed, answered after the process that armed them died.

WHY THIS FILE EXISTS
--------------------
Two of the bot's prompts are armed ONLY in ``context.user_data``:

* ``awaiting_otp`` — set when the verification SMS goes out
  (``handlers/profile.py``)
* ``awaiting_location`` — set by ``utils.arm_location_request`` at every site
  that shows the pin keyboard, including the zero-address checkout screen, which
  arms it WITHOUT entering any conversation

Both prompts are answered by something client-side that outlives any restart: a
6-digit code the customer is reading off an SMS, and a `request_location` reply
keyboard sitting on their phone. So a deploy between the prompt and the answer
leaves the marker gone and the prompt still on screen.

What used to happen then is the same in both cases, and it is worse than an
error. ``bot.py::_handle_text_message`` and ``_route_address_location_entry``
both fall through to ``capture_support_message``, which is SILENT BY DESIGN — so:

* the customer's live OTP is filed, unredacted, into the admin Support Inbox as
  an unsolicited message, and the phone is never verified;
* the customer's home coordinates are filed there too, no address is created,
  and checkout stays stuck behind the address they thought they had just added.

Either way the customer is told nothing at all.

The durable half already exists and was simply unused for these two:
``BotUserRepository.arm_awaiting_input`` writes ``users.bot_state``, which
survives restarts, and ``_handle_text_message`` already reads it four lines
below the branch that failed.
"""

import json

import pytest

from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


PROFILE = "/api/v1/auth/profile"
SEND_OTP = "/api/v1/auth/send-otp"
VERIFY_OTP = "/api/v1/auth/verify-phone"
SUPPORT = "/api/v1/support/messages"

TRANSLATIONS = {
    "telegram.phone.verification_code_sent": "CODE-SENT",
    "telegram.error_occurred": "SOMETHING-WENT-WRONG",
}


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    # `verify_phone_number` reads the number off the profile and bails out early
    # without one, so the SMS would never be "sent" and nothing would be armed.
    harness.backend.route(
        "GET", PROFILE,
        lambda _c: {"data": {
            "id": 398, "phone": "+998978730111", "phone_verified": False,
            "first_name": "Kamola",
        }},
    )
    harness.backend.route("POST", SEND_OTP, lambda _c: {"data": {"sent": True}})
    harness.backend.route("POST", VERIFY_OTP, lambda _c: {"data": {"verified": True}})
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


def restart(bot):
    """A deploy: `user_data` and every conversation state die with the process.
    `users.bot_state` — a DB column — does not."""
    bot.application.user_data[DEFAULT_USER_ID].clear()
    for group in bot.application.handlers.values():
        for handler in group:
            conversations = getattr(handler, "_conversations", None)
            if conversations is not None:
                conversations.clear()
    bot.telegram.reset()


def support_posts(bot):
    return [c.data for c in bot.backend.calls if c.endpoint == SUPPORT]


def bot_state(bot) -> dict:
    raw = bot.database.user.get("bot_state") or "{}"
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


async def arm_the_otp(bot, user):
    """Drive the real 'Verify phone number' tap so the SMS really goes out."""
    await bot.send(user.tap("verify_phone_number"))
    return bot


# ---------------------------------------------------------------------------
# The OTP
# ---------------------------------------------------------------------------


async def test_arming_the_otp_is_written_somewhere_that_survives_a_restart(bot, user):
    """`user_data` is process memory. The arming has to reach `users.bot_state`
    or nothing downstream can tell an OTP from a support message."""
    await arm_the_otp(bot, user)

    assert bot_state(bot).get("awaiting_input") == "phone_otp", (
        f"the OTP arming never reached the durable bot_state: {bot_state(bot)}"
    )


async def test_a_code_typed_after_a_restart_is_not_filed_as_a_support_message(bot, user):
    """The sharp end: a live one-time password, unredacted, in the admin inbox."""
    await arm_the_otp(bot, user)
    restart(bot)

    await bot.send(user.text("123456"))

    leaked = [p for p in support_posts(bot) if "123456" in json.dumps(p)]
    assert not leaked, (
        f"the customer's OTP was filed into the Support Inbox: {leaked}"
    )


async def test_a_code_typed_after_a_restart_still_verifies_the_phone(bot, user):
    """Not leaking it is only half the fix — the code must still do its job."""
    await arm_the_otp(bot, user)
    restart(bot)

    await bot.send(user.text("123456"))

    verified = [
        c for c in bot.backend.calls
        if c.endpoint == VERIFY_OTP and c.method == "POST"
    ]
    assert verified, (
        "the code was never submitted for verification; the bot called "
        f"{[c.endpoint for c in bot.backend.calls]}"
    )


async def test_ordinary_free_text_is_still_captured_as_support(bot, user):
    """The guard must not swallow the Support Inbox: text with nothing armed is
    still a support message."""
    await bot.send(user.text("my delivery never arrived"))

    assert support_posts(bot), (
        "free text with no armed prompt stopped reaching the Support Inbox"
    )


# ---------------------------------------------------------------------------
# The location pin
# ---------------------------------------------------------------------------


async def test_a_pin_shared_after_a_restart_is_not_filed_as_a_support_message(bot, user):
    """`arm_location_request` is the ONLY thing that distinguishes a pin the bot
    asked for from a spontaneous one. It lived in `user_data` alone, so after a
    deploy the customer's home coordinates went to the admin inbox instead of
    becoming their delivery address."""
    await bot.send(user.tap("add_new_address"))
    restart(bot)

    await bot.send(user.location(41.2876, 69.2224))

    coordinates_in_support = [
        p for p in support_posts(bot)
        if "41.2876" in json.dumps(p) or "location" in json.dumps(p).lower()
    ]
    assert not coordinates_in_support, (
        f"the pin was filed as an unsolicited support message: {coordinates_in_support}"
    )


async def test_a_spontaneous_pin_is_still_treated_as_support(bot, user):
    """The other half of the rule, and the reason the marker exists at all: a pin
    nobody asked for is a support message, not an address."""
    await bot.send(user.location(41.3000, 69.3000))

    assert support_posts(bot), (
        "an unprompted pin stopped being captured for the Support Inbox"
    )
