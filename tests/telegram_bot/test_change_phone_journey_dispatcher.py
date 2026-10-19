"""Profile → Phone → "Change number" with a TYPED number, driven through the REAL dispatcher.

WHY THIS FILE EXISTS
--------------------
A typed number proves nothing about who holds the SIM. This flow used to write
it straight into ``users.phone`` over the bot's own SQL: no SMS, and
``phone_verified_at`` left as it was, so a customer whose old number was
verified now showed a stranger's number as VERIFIED. The real owner was then
locked out: ``/send-otp`` answered 409, phone registration said "taken", and the
link offer was refused. That is the "claim a number you do not own" hole the
owner asked to close ("a number is saved only after its code is verified").

The typed number now goes through the same ``/send-otp`` → code →
``/verify-phone`` path as registration's typed branch, and only the backend's
verify step writes it. The name step that followed the phone step is kept.

Every update goes in through ``Application.process_update``; only Telegram, the
backend and the bot's own SQL are faked (``tests/telegram_bot/ptb_harness.py``).
"""

import pytest
from telegram.ext import CallbackContext, ConversationHandler

import utils as utils_module
from handlers.profile import PHONE_VERIFY_NAME, PHONE_VERIFY_OTP, PHONE_VERIFY_PHONE

from tests.telegram_bot.ptb_harness import (
    DEFAULT_USER_ID,
    backend_failure,
    build_bot_harness,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


SEND_OTP = "/api/v1/auth/send-otp"
VERIFY_PHONE = "/api/v1/auth/verify-phone"
PROFILE = "/api/v1/auth/profile"
SUPPORT_MESSAGES = "/api/v1/support/messages"

# The harness's customer already holds +998978730111 (verified or not, it does
# not matter here). The number they type is somebody else's until the code
# read off ITS SMS proves otherwise.
NEW_PHONE_E164 = "+998935554433"
MASKED = "+99***4433"

KEYBOARD_CLEANUP_FILLER = "⁠"

TRANSLATIONS = {
    "telegram.phone.send_code_prompt": "Share your phone to receive a code",
    "telegram.profile.share_phone": "SHARE-CONTACT-BUTTON",
    "telegram.phone.phone_accepted": "Phone accepted",
    "telegram.phone.invalid_format": "That is not a valid Uzbek mobile number",
    "telegram.phone.verification_code_sent_to_phone_prompt": "Code sent to {phone_masked}",
    "telegram.phone.verification_sms_send_failed": "Could not send the SMS: {error}",
    "telegram.phone.already_registered_use_different": (
        "This number is already registered. Use a different one"
    ),
    "telegram.bot.otp.invalid_format": "The code must be 6 digits",
    "telegram.bot.otp.auth_error": "Sign-in problem, try later",
    "telegram.phone.verification_failed_with_error_retry": "Wrong code ({error}). Try again",
    "telegram.phone.verification_failed_retry": "Verification failed. Try again",
    "telegram.phone.verification_flow_timed_out": (
        "Phone confirmation was closed. Your number was not added"
    ),
    "telegram.phone.phone_verified": "Phone number is verified",
    "telegram.enter_name": "Enter your full name",
    "telegram.profile_updated": "Profile updated",
    "telegram.action_cancelled_short": "Cancelled",
    "telegram.action_cancelled": "Cancelled. Here is the menu",
    "telegram.cancel": "Cancel",
}


def _always(value):
    async def _answer(*_args, **_kwargs):
        return value

    return _answer


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    # Answered from memory, not the shared test Redis: "allowed" is the
    # production-normal answer, and one test's traffic must not decide another's.
    monkeypatch.setattr(utils_module.rate_limiter, "allow_request", _always(True))
    harness.backend.route(
        "POST", SEND_OTP, lambda _c: {"data": {"phone_masked": MASKED}}
    )
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


def texts(bot):
    return [
        call.text
        for call in bot.telegram.shown
        if call.text and call.text != KEYBOARD_CLEANUP_FILLER
    ]


def last_text(bot):
    shown = texts(bot)
    assert shown, "the bot showed the customer nothing at all"
    return shown[-1]


def calls(bot, method, endpoint):
    return [c for c in bot.backend.calls if c.method == method and c.endpoint == endpoint]


def payloads(bot, method, endpoint):
    return [c.data for c in calls(bot, method, endpoint)]


def state(bot):
    return bot.conversation_state("phone_verification")


def flow_data(bot):
    return bot.application.user_data.get(DEFAULT_USER_ID, {})


def phone_writes(bot):
    """Every statement the bot ran itself that touches ``users.phone``.

    Empty is the whole point: the backend's ``/verify-phone`` is the only
    writer allowed, and it writes only after the code matches.
    """
    return [q for q in bot.database.executed if "SET phone" in q]


def profile_phone_writes(bot):
    return [d for d in payloads(bot, "PUT", PROFILE) if d and "phone" in d]


async def reach_typed_phone_step(bot, user):
    await bot.send(user.tap("add_phone_number"))
    assert state(bot) == PHONE_VERIFY_PHONE
    bot.telegram.reset()


async def reach_code_step(bot, user):
    await reach_typed_phone_step(bot, user)
    await bot.send(user.text(NEW_PHONE_E164))
    assert state(bot) == PHONE_VERIFY_OTP
    bot.telegram.reset()


async def fire_conversation_timeout(bot, carrier):
    """Run the flow's TIMEOUT step the way PTB does when its timer fires."""
    handler = bot.conversation("phone_verification")
    context = CallbackContext.from_update(carrier, bot.application)
    for inner in handler.states.get(ConversationHandler.TIMEOUT) or []:
        check = inner.check_update(carrier)
        if check is None or check is False:
            continue
        return await inner.handle_update(carrier, bot.application, check, context)
    raise AssertionError("no TIMEOUT handler in 'phone_verification' accepts the update")


# ===========================================================================
# The typed number is saved only by the verify step
# ===========================================================================


async def test_a_typed_number_is_saved_only_after_its_code_and_the_name_step_still_follows(
    bot, user
):
    await reach_typed_phone_step(bot, user)

    await bot.send(user.text(NEW_PHONE_E164))

    assert payloads(bot, "POST", SEND_OTP) == [{"phone": NEW_PHONE_E164}]
    assert calls(bot, "POST", SEND_OTP)[0].user_token == "test-access-token", (
        "/send-otp is a JWT route; the code must be sent for THIS customer"
    )
    assert phone_writes(bot) == [], "the typed number reached users.phone with no code"
    assert profile_phone_writes(bot) == []
    assert state(bot) == PHONE_VERIFY_OTP
    assert "Code sent to " + MASKED in texts(bot), (
        "the prompt must echo the backend's MASKED number"
    )

    # Fat-fingered: five digits cost no backend call.
    bot.telegram.reset()
    await bot.send(user.text("12345"))
    assert last_text(bot) == "The code must be 6 digits"
    assert calls(bot, "POST", VERIFY_PHONE) == []
    assert state(bot) == PHONE_VERIFY_OTP

    # Right shape, wrong code: one retry, not the end of the flow.
    bot.backend.route(
        "POST", VERIFY_PHONE,
        lambda _c: backend_failure("Invalid verification code", status_code=400),
    )
    bot.telegram.reset()
    await bot.send(user.text("999999"))
    assert payloads(bot, "POST", VERIFY_PHONE) == [{"otp": "999999"}]
    assert last_text(bot) == "Wrong code (Invalid verification code). Try again"
    assert state(bot) == PHONE_VERIFY_OTP
    assert phone_writes(bot) == []

    # The code off the SMS: the backend binds the number, the bot asks the name.
    bot.backend.route("POST", VERIFY_PHONE, lambda _c: {"data": {"phone": NEW_PHONE_E164}})
    bot.telegram.reset()
    await bot.send(user.text("123456"))
    assert payloads(bot, "POST", VERIFY_PHONE) == [{"otp": "999999"}, {"otp": "123456"}]
    assert texts(bot) == ["Phone number is verified", "Enter your full name"]
    assert state(bot) == PHONE_VERIFY_NAME
    assert "pending_phone_verification" not in flow_data(bot)

    bot.telegram.reset()
    await bot.send(user.text("Kamola Karimova"))
    assert payloads(bot, "PUT", PROFILE) == [{"first_name": "Kamola", "last_name": "Karimova"}]
    assert last_text(bot) == "Profile updated"
    assert state(bot) is None

    assert phone_writes(bot) == [], "the bot itself must never write the typed number"
    assert calls(bot, "POST", SUPPORT_MESSAGES) == [], (
        "neither the typed number nor the live code may land in the Support Inbox"
    )


@pytest.mark.parametrize(
    "status,error,expected",
    [
        (409, "Email already registered", "This number is already registered. Use a different one"),
        (500, "SMS gateway down", "Could not send the SMS: SMS gateway down"),
    ],
    ids=["number-held-by-another-account", "sms-not-sent"],
)
async def test_a_refused_code_request_keeps_the_customer_on_the_phone_step(
    bot, user, status, error, expected
):
    bot.backend.route(
        "POST", SEND_OTP, lambda _c: backend_failure(error, status_code=status)
    )
    await reach_typed_phone_step(bot, user)

    await bot.send(user.text(NEW_PHONE_E164))

    assert payloads(bot, "POST", SEND_OTP) == [{"phone": NEW_PHONE_E164}]
    assert last_text(bot) == expected
    assert state(bot) == PHONE_VERIFY_PHONE, "they can still type another number"
    assert "pending_phone_verification" not in flow_data(bot)
    assert phone_writes(bot) == []
    assert profile_phone_writes(bot) == []


async def test_a_number_the_validator_rejects_never_costs_an_sms(bot, user):
    await reach_typed_phone_step(bot, user)

    await bot.send(user.text("+7 916 123 45 67"))

    assert calls(bot, "POST", SEND_OTP) == []
    assert last_text(bot) == "That is not a valid Uzbek mobile number"
    assert state(bot) == PHONE_VERIFY_PHONE


# ===========================================================================
# Leaving the code step drops the pending number
# ===========================================================================


async def test_cancel_at_the_code_step_drops_the_pending_number(bot, user):
    await reach_code_step(bot, user)
    assert flow_data(bot).get("pending_phone_verification") == NEW_PHONE_E164

    await bot.send(user.command("/cancel"))

    assert state(bot) is None
    assert "pending_phone_verification" not in flow_data(bot)
    assert calls(bot, "POST", VERIFY_PHONE) == []
    assert phone_writes(bot) == []


async def test_a_timed_out_code_step_says_so_and_drops_the_pending_number(bot, user):
    await reach_code_step(bot, user)

    await fire_conversation_timeout(bot, user.text("123456"))

    assert "Phone confirmation was closed. Your number was not added" in texts(bot)
    assert "pending_phone_verification" not in flow_data(bot)
    assert calls(bot, "POST", VERIFY_PHONE) == []
    assert phone_writes(bot) == []
