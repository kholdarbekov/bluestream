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

import json
from datetime import datetime, timedelta, timezone

import pytest
from telegram.ext import CallbackContext, ConversationHandler

import utils as utils_module
from handlers.profile import PHONE_VERIFY_NAME, PHONE_VERIFY_OTP, PHONE_VERIFY_PHONE
from shared.business_config import PHONE_OTP_EXPIRY

from tests.telegram_bot.ptb_harness import (
    DEFAULT_USER_ID,
    backend_failure,
    build_bot_harness,
)
from tests.telegram_bot.menu_keyboard_helpers import expected_labels, menu_sends
from tests.telegram_bot.test_menu_label_routing import MENU_LABEL_TRANSLATIONS

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

TRANSLATIONS = {
    **MENU_LABEL_TRANSLATIONS,
    "telegram.support.menu_intro": "Help screen",
    "telegram.help.command_hint": "Use the buttons below",
    "telegram.bot.otp.success_message": "Phone verified by code",
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
        if call.text
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


def bot_state(bot):
    return json.loads(bot.database.user.get("bot_state") or "{}")


@pytest.mark.parametrize("typed", ["Kod kelmadi", "12345"], ids=["words", "five-digits"])
async def test_text_that_is_not_a_code_ends_the_code_step_and_the_code_stays_live(bot, user, typed):
    """Spec §2 "Other text": no endless 'the code must be 6 digits' loop. The step
    ends and the message goes where any other message goes, but the code stays
    answerable for the rest of its lifetime ("Why"): the real code typed next
    verifies the number it was sent to and never lands in the Support Inbox."""
    await reach_code_step(bot, user)
    sent_at = flow_data(bot)["phone_otp_sent_at"]

    await bot.send(user.text(typed))

    assert state(bot) is None
    assert "pending_phone_verification" not in flow_data(bot)
    assert calls(bot, "POST", VERIFY_PHONE) == []
    assert payloads(bot, "POST", SUPPORT_MESSAGES) == [{"content": typed, "message_type": "text"}]
    assert texts(bot) == [], "support capture is silent"
    durable = bot_state(bot)
    assert durable["awaiting_input"] == "phone_otp"
    assert durable["pending_phone_verification"] == NEW_PHONE_E164
    assert durable["phone_otp_armed_at"] == sent_at, "the lifetime counts from the SMS"

    bot.backend.route("POST", VERIFY_PHONE, lambda _c: {"data": {"phone": NEW_PHONE_E164}})
    await bot.send(user.text("123456"))

    assert payloads(bot, "POST", SEND_OTP) == [{"phone": NEW_PHONE_E164}]
    assert payloads(bot, "POST", VERIFY_PHONE) == [{"otp": "123456"}]
    assert last_text(bot) == "Phone verified by code"
    assert "awaiting_input" not in bot_state(bot)
    assert payloads(bot, "POST", SUPPORT_MESSAGES) == [{"content": typed, "message_type": "text"}]
    assert phone_writes(bot) == []


async def test_text_at_the_code_step_after_the_code_lifetime_lets_nothing_verify_later(bot, user):
    await reach_code_step(bot, user)
    flow_data(bot)["phone_otp_sent_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=PHONE_OTP_EXPIRY + 1)
    ).isoformat()

    await bot.send(user.text("12345"))
    await bot.send(user.text("123456"))

    assert state(bot) is None
    assert calls(bot, "POST", VERIFY_PHONE) == []
    assert "awaiting_input" not in bot_state(bot)
    assert payloads(bot, "POST", SUPPORT_MESSAGES) == [
        {"content": "12345", "message_type": "text"},
        {"content": "123456", "message_type": "text"},
    ]


async def test_a_code_typed_after_leaving_the_code_step_still_verifies_the_number(bot, user):
    """Spec §2 "Late codes": the customer tapped a menu button while the SMS was on
    its way. The code they then type is still checked against the number it was
    sent to, and never lands in the Support Inbox."""
    await reach_code_step(bot, user)
    sent_at = flow_data(bot)["phone_otp_sent_at"]

    await bot.send(user.text("🆘 Yordam"))

    assert state(bot) is None
    assert last_text(bot) == "Help screen\n\nUse the buttons below"
    durable = bot_state(bot)
    assert durable["awaiting_input"] == "phone_otp"
    assert durable["pending_phone_verification"] == NEW_PHONE_E164
    assert durable["phone_otp_armed_at"] == sent_at, "the lifetime counts from the SMS, not the tap"

    bot.backend.route("POST", VERIFY_PHONE, lambda _c: {"data": {"phone": NEW_PHONE_E164}})
    bot.telegram.reset()
    await bot.send(user.text("123456"))

    assert payloads(bot, "POST", SEND_OTP) == [{"phone": NEW_PHONE_E164}]
    assert payloads(bot, "POST", VERIFY_PHONE) == [{"otp": "123456"}]
    assert last_text(bot) == "Phone verified by code"
    assert "awaiting_input" not in bot_state(bot)
    assert calls(bot, "POST", SUPPORT_MESSAGES) == []
    assert phone_writes(bot) == []


async def test_leaving_the_code_step_late_does_not_stretch_the_code_lifetime(bot, user):
    """The SMS went out PHONE_OTP_EXPIRY + 1 s before the customer tapped away. The
    exit must not give that code a fresh lifetime."""
    await reach_code_step(bot, user)
    sent_at = (datetime.now(timezone.utc) - timedelta(seconds=PHONE_OTP_EXPIRY + 1)).isoformat()
    flow_data(bot)["phone_otp_sent_at"] = sent_at

    await bot.send(user.text("🆘 Yordam"))

    assert bot_state(bot)["phone_otp_armed_at"] == sent_at

    bot.telegram.reset()
    await bot.send(user.text("123456"))

    assert calls(bot, "POST", VERIFY_PHONE) == []
    assert "awaiting_input" not in bot_state(bot)
    assert payloads(bot, "POST", SUPPORT_MESSAGES) == [{"content": "123456", "message_type": "text"}]


async def test_a_code_written_with_a_space_at_the_code_step_is_verified_as_its_digits(bot, user):
    """One rule for "is this a code" (utils.otp_code_in): the code step verifies the
    digits of "123 456", and the flow goes on to the name step."""
    await reach_code_step(bot, user)
    bot.backend.route("POST", VERIFY_PHONE, lambda _c: {"data": {"phone": NEW_PHONE_E164}})

    await bot.send(user.text("123 456"))

    assert payloads(bot, "POST", VERIFY_PHONE) == [{"otp": "123456"}]
    assert texts(bot) == ["Phone number is verified", "Enter your full name"]
    assert state(bot) == PHONE_VERIFY_NAME
    assert calls(bot, "POST", SUPPORT_MESSAGES) == []


async def test_the_share_contact_step_offers_cancel_and_cancel_puts_the_menu_back(bot, user):
    await bot.send(user.tap("add_phone_number"))
    assert bot.telegram.last_shown().reply_markup["keyboard"] == [
        [{"text": "SHARE-CONTACT-BUTTON", "request_contact": True}],
        [{"text": "Cancel"}],
    ]
    bot.telegram.reset()

    await bot.send(user.text("Cancel"))

    assert state(bot) is None
    assert bot.conversation_state("address_conversation") is None, "the address form's own Cancel must not claim it"
    assert texts(bot) == ["Cancelled. Here is the menu"]
    [carrier] = menu_sends(bot)
    assert carrier.button_labels() == expected_labels()


@pytest.mark.parametrize("how,next_prompt", [
    ("contact", "Enter your full name"),
    ("typed", f"Code sent to {MASKED}"),
])
async def test_an_accepted_number_puts_the_menu_back_before_the_next_question(bot, user, how, next_prompt):
    await reach_typed_phone_step(bot, user)

    await bot.send(user.contact("998978730111") if how == "contact" else user.text(NEW_PHONE_E164))

    [carrier] = menu_sends(bot)
    assert (carrier.text, carrier.button_labels()) == ("Phone accepted", expected_labels())
    assert texts(bot)[-1] == next_prompt
