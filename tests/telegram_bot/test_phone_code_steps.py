# tests/telegram_bot/test_phone_code_steps.py
"""Phone-code steps (spec §2): the profile "verify phone" code prompt.

A code typed within its lifetime still verifies after the customer tapped
elsewhere. Text that is not a code ends the step and is handled normally. After
PHONE_OTP_EXPIRY the prompt disarms itself. The prompt has a Cancel button.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import utils as utils_module
from business_app.services.notification_service import OTP_SMS_TEMPLATES
from shared.business_config import PHONE_OTP_EXPIRY

from tests.telegram_bot.menu_keyboard_helpers import expected_labels, menu_sends
from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, build_bot_harness
from tests.telegram_bot.test_menu_label_routing import MENU_LABELS, MENU_LABEL_TRANSLATIONS

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PROFILE = "/api/v1/auth/profile"
SEND_OTP = "/api/v1/auth/send-otp"
VERIFY_OTP = "/api/v1/auth/verify-phone"
SUPPORT = "/api/v1/support/messages"
PROFILE_PHONE = "+998978730111"  # the harness customer's number, not yet verified here
CODE = "482913"
HELP_LABEL = MENU_LABELS["telegram.menu.support"]["uz"]

TRANSLATIONS = {
    **MENU_LABEL_TRANSLATIONS,
    "telegram.phone.verification_sms_sent": "Kod {phone} raqamiga yuborildi",
    "telegram.bot.otp.success_message": "TELEFON-TASDIQLANDI",
    "telegram.support.menu_intro": "YORDAM-EKRANI",
    "telegram.help.command_hint": "PASTDAGI-TUGMALAR",
    "telegram.cancel": "❌ Bekor qilish",
    "telegram.action_cancelled": "BEKOR-QILINDI",
    "telegram.support.describe_issue_prompt": "MUAMMONI-YOZING {order_number}",
    "telegram.support.ack": "MUROJAAT-QABUL-QILINDI",
}


def _always(value):
    async def _answer(*_args, **_kwargs):
        return value

    return _answer


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    monkeypatch.setattr(utils_module.rate_limiter, "allow_request", _always(True))
    harness.backend.route(
        "GET", PROFILE,
        lambda _c: {"data": {"id": 398, "phone": PROFILE_PHONE, "phone_verified": False, "first_name": "Kamola"}},
    )
    harness.backend.route("POST", SEND_OTP, lambda _c: {"data": {"sent": True}})
    harness.backend.route("POST", VERIFY_OTP, lambda _c: {"data": {"verified": True}})
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


def bot_state(bot):
    return json.loads(bot.database.user.get("bot_state") or "{}")


def calls(bot, endpoint):
    return [c for c in bot.backend.calls if c.method == "POST" and c.endpoint == endpoint]


def support_posts(bot):
    return [c.data for c in calls(bot, SUPPORT)]


async def arm_the_profile_code(bot, user):
    await bot.send(user.tap("verify_phone_number"))
    assert [c.data for c in calls(bot, SEND_OTP)] == [{"phone": PROFILE_PHONE}]
    assert bot_state(bot)["awaiting_input"] == "phone_otp"
    bot.telegram.reset()
    bot.backend.calls.clear()


def age_the_code(bot, seconds):
    """Back-date the stamp production reads, as if the code was sent `seconds` ago."""
    state = bot_state(bot)
    state["phone_otp_armed_at"] = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    bot.database.user["bot_state"] = json.dumps(state)


async def test_arming_the_code_prompt_records_when_and_offers_cancel(bot, user):
    before = datetime.now(timezone.utc)

    await bot.send(user.tap("verify_phone_number"))

    state = bot_state(bot)
    assert state["awaiting_input"] == "phone_otp"
    assert state["pending_phone_verification"] == PROFILE_PHONE
    assert before <= datetime.fromisoformat(state["phone_otp_armed_at"]) <= datetime.now(timezone.utc)
    prompt = bot.telegram.last_shown()
    assert prompt.callback_data() == ["phone_otp_cancel"]
    assert prompt.button_labels() == ["❌ Bekor qilish"]


async def test_a_label_tap_leaves_the_code_armed_and_a_code_typed_afterwards_verifies(bot, user):
    await arm_the_profile_code(bot, user)

    await bot.send(user.text(HELP_LABEL))
    assert bot.telegram.last_shown().text == "YORDAM-EKRANI\n\nPASTDAGI-TUGMALAR"
    assert bot_state(bot)["awaiting_input"] == "phone_otp", "an exit never disarms the code"

    await bot.send(user.text(CODE))

    assert [c.data for c in calls(bot, VERIFY_OTP)] == [{"otp": CODE}]
    assert calls(bot, VERIFY_OTP)[0].user_token == "test-access-token"
    assert bot.telegram.last_shown().text == "TELEFON-TASDIQLANDI"
    assert "awaiting_input" not in bot_state(bot)
    assert support_posts(bot) == []


async def test_text_that_is_not_a_code_reaches_support_and_the_code_stays_live(bot, user):
    """Spec §2 "Other text" and "Why": the text is handled like any other message,
    with no 'invalid code' reply, and the prompt stays armed for the rest of the
    code's lifetime, so the SMS that arrives afterwards still verifies."""
    await arm_the_profile_code(bot, user)

    await bot.send(user.text("Kod kelmadi"))

    assert support_posts(bot) == [{"content": "Kod kelmadi", "message_type": "text"}]
    assert calls(bot, VERIFY_OTP) == []
    assert bot.telegram.shown == [], "no endless 'invalid code format' reply; capture is silent"
    state = bot_state(bot)
    assert state["awaiting_input"] == "phone_otp"
    assert state["pending_phone_verification"] == PROFILE_PHONE

    await bot.send(user.text(CODE))

    assert [c.data for c in calls(bot, VERIFY_OTP)] == [{"otp": CODE}]
    assert calls(bot, VERIFY_OTP)[0].user_token == "test-access-token"
    assert bot.telegram.last_shown().text == "TELEFON-TASDIQLANDI"
    assert "awaiting_input" not in bot_state(bot)
    assert support_posts(bot) == [{"content": "Kod kelmadi", "message_type": "text"}], (
        "the live code must never reach the Support Inbox"
    )


async def test_after_the_code_lifetime_text_then_a_code_both_route_normally(bot, user):
    await arm_the_profile_code(bot, user)
    age_the_code(bot, PHONE_OTP_EXPIRY + 1)

    await bot.send(user.text("Kod kelmadi"))
    await bot.send(user.text(CODE))

    assert calls(bot, VERIFY_OTP) == []
    assert "awaiting_input" not in bot_state(bot)
    assert support_posts(bot) == [
        {"content": "Kod kelmadi", "message_type": "text"},
        {"content": CODE, "message_type": "text"},
    ]


async def test_a_prompt_armed_after_the_code_prompt_gets_the_text(bot, user):
    """The newer prompt wins: "Report an issue" tapped on an order card after the
    code was sent. The text is the concern, never re-read as a code step's."""
    order_id, order_number = 555, "TG_000555_26"
    bot.backend.route(
        "GET", f"/api/v1/orders/{order_id}",
        lambda _c: {"data": {"order": {"id": order_id, "order_number": order_number}}},
    )
    await arm_the_profile_code(bot, user)
    assert bot.application.user_data[DEFAULT_USER_ID]["awaiting_otp"] is True

    await bot.send(user.tap(f"report_issue_{order_id}"))
    assert bot_state(bot)["awaiting_input"] == "support_message"

    complaint = "Suv idishi ochilgan holda keldi"
    await bot.send(user.text(complaint))

    assert support_posts(bot) == [{"content": f"[Order #{order_number}] {complaint}", "message_type": "text"}]
    assert bot.telegram.last_shown().text == "MUROJAAT-QABUL-QILINDI"
    assert calls(bot, VERIFY_OTP) == []
    assert bot_state(bot).get("awaiting_input") != "phone_otp", "the code prompt never overwrites a newer prompt"


async def test_a_code_typed_after_the_code_lifetime_is_not_verified_and_is_routed_normally(bot, user):
    await arm_the_profile_code(bot, user)
    age_the_code(bot, PHONE_OTP_EXPIRY + 1)

    await bot.send(user.text(CODE))

    assert calls(bot, VERIFY_OTP) == []
    assert "awaiting_input" not in bot_state(bot)
    assert support_posts(bot) == [{"content": CODE, "message_type": "text"}]


async def test_a_code_typed_just_inside_the_lifetime_still_verifies(bot, user):
    await arm_the_profile_code(bot, user)
    age_the_code(bot, PHONE_OTP_EXPIRY - 5)

    await bot.send(user.text(CODE))

    assert [c.data for c in calls(bot, VERIFY_OTP)] == [{"otp": CODE}]


async def test_a_code_prompt_armed_before_this_release_still_takes_its_code(bot, user):
    """No `phone_otp_armed_at`: armed by the pre-release code, or the durable arm write failed.
    The prompt is stamped on first sight, never counted as expired, so a live code is verified
    and never lands in the Support Inbox (spec §2 "Late codes / Why")."""
    bot.database.user["bot_state"] = json.dumps(
        {"awaiting_input": "phone_otp", "pending_phone_verification": PROFILE_PHONE})

    await bot.send(user.text(CODE))

    assert [c.data for c in calls(bot, VERIFY_OTP)] == [{"otp": CODE}]
    assert support_posts(bot) == []


async def test_cancel_on_the_code_prompt_disarms_it_and_shows_the_menu(bot, user):
    await arm_the_profile_code(bot, user)

    await bot.send(user.tap("phone_otp_cancel"))

    assert "awaiting_input" not in bot_state(bot)
    assert {"awaiting_otp", "pending_phone_verification"}.isdisjoint(
        bot.application.user_data[DEFAULT_USER_ID]
    )
    assert bot.telegram.last_shown().text == "BEKOR-QILINDI"
    [carrier] = menu_sends(bot)
    assert (carrier.text, carrier.button_labels()) == ("BEKOR-QILINDI", expected_labels())


# --- What counts as a code (controller ruling on Task 13, spec §2 "Late codes / Why") ---
# A live code must never land in the Support Inbox, and the old prompt let the customer
# retry a code written loosely, so a code with separators or inside a pasted SMS is still
# verified, as its digits.


@pytest.mark.parametrize("typed", ["482 913", "482-913"], ids=["space", "hyphen"])
async def test_a_code_written_with_a_space_or_hyphen_is_verified_as_its_digits(bot, user, typed):
    await arm_the_profile_code(bot, user)

    await bot.send(user.text(typed))

    assert [c.data for c in calls(bot, VERIFY_OTP)] == [{"otp": CODE}]
    assert bot.telegram.last_shown().text == "TELEFON-TASDIQLANDI"
    assert support_posts(bot) == []


@pytest.mark.parametrize(
    "pasted",
    [
        f"Kod: {CODE}",
        f"Kod:{CODE}",
        *(OTP_SMS_TEMPLATES["sms.verification.otp"][language].format(otp_code=CODE) for language in ("uz", "ru", "en")),
    ],
    ids=["short", "no-space", "the-seeded-sms-uz", "the-seeded-sms-ru", "the-seeded-sms-en"],
)
async def test_a_pasted_sms_is_verified_with_the_one_code_it_carries(bot, user, pasted):
    await arm_the_profile_code(bot, user)

    await bot.send(user.text(pasted))

    assert [c.data for c in calls(bot, VERIFY_OTP)] == [{"otp": CODE}]
    assert bot.telegram.last_shown().text == "TELEFON-TASDIQLANDI"
    assert support_posts(bot) == []


# Order, try-out and delivery numbers carry six digits between word characters or a hyphen
# (`Order` "TG_000413_26", `tryout_service` "TRY_000123_26", `admin_delivery_service` "DLV-000123").
ORDER_NUMBER_SHAPED = ["TG_000413_26", "TRY_000123_26", "DLV-000123", "Buyurtmam TG_000413_26 qayerda?"]


@pytest.mark.parametrize("pasted", ORDER_NUMBER_SHAPED)
async def test_a_pasted_order_number_is_not_a_code(bot, user, pasted):
    """Within a code's lifetime a pasted order number is an ordinary message, never a code try."""
    await arm_the_profile_code(bot, user)

    await bot.send(user.text(pasted))

    assert calls(bot, VERIFY_OTP) == []
    assert support_posts(bot) == [{"content": pasted, "message_type": "text"}]
    assert bot_state(bot)["awaiting_input"] == "phone_otp", "other text never disarms a live code"


@pytest.mark.parametrize("text,code", [
    *((pasted, None) for pasted in ORDER_NUMBER_SHAPED),
    (CODE, CODE), ("482 913", CODE), ("482-913", CODE), (f"Kod: {CODE}", CODE), (f"Kod:{CODE}", CODE),
    (f"Kod: {CODE}.", CODE), (f"({CODE})", CODE),
    *((OTP_SMS_TEMPLATES["sms.verification.otp"][language].format(otp_code=CODE), CODE) for language in ("uz", "ru", "en")),
])
def test_what_otp_code_in_reads_as_a_code(text, code):
    """The one rule every code step asks (`utils.otp_code_in`), on each shape at once."""
    assert utils_module.otp_code_in(text) == code


async def test_a_message_with_two_six_digit_numbers_is_not_a_code(bot, user):
    typed = f"{CODE} yoki 731406?"
    await arm_the_profile_code(bot, user)

    await bot.send(user.text(typed))

    assert calls(bot, VERIFY_OTP) == []
    assert support_posts(bot) == [{"content": typed, "message_type": "text"}]
    assert bot_state(bot)["awaiting_input"] == "phone_otp", "other text never disarms a live code"
