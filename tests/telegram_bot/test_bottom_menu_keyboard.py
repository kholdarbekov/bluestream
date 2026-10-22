"""The persistent bottom keyboard (spec 2026-10-21 §1), driven through the real dispatcher.

Tasks 14-18 of docs/superpowers/plans/2026-10-21-customer-bot-bottom-menu-and-reorder.md add to
this file in order. The copy is the real seed, so a test reads what a customer reads. Only
Telegram, the backend and the bot's own SQL are faked (tests/telegram_bot/ptb_harness.py).
"""

import json

import pytest
from telegram.error import BadRequest

import database as database_module
import utils as utils_module
from handlers.base import BaseHandler
from handlers.profile import (
    ADDRESS_LOCATION, ADDRESS_REGION, PHONE, PHONE_VERIFY_NAME, PHONE_VERIFY_OTP, PHONE_VERIFY_PHONE, REGISTER_OTP,
)
from keyboards import MAIN_MENU_KEYS, MenuKeyboards
from menu_keyboard import MENU_KEYBOARD_STATE_KEY, installed_menu_labels, send_menu_keyboard, wanted_menu_labels
from tests.telegram_bot.helpers import DummyCallbackQuery
from tests.telegram_bot.menu_keyboard_helpers import (
    RU_LABELS, SEEDED, UZ_LABELS, UZ_ROWS, UZ_ROWS_WITHOUT_AQUA_CLUB,
    assert_no_edit_carries_a_reply_keyboard, expected_labels, menu_sends, seeded, stored_marker,
)
from tests.telegram_bot.ptb_harness import (
    DEFAULT_CHAT_ID, DEFAULT_USER_ID, _install_translations, build_bot_harness, menu_already_installed,
)
from tests.telegram_bot.test_manual_address_entry_journey import UZ as MANUAL_ENTRY_COPY, walk_to_the_street_prompt
from tests.telegram_bot.test_registration_journey_dispatcher import (  # noqa: F401  (make_bot is a fixture)
    MY_PHONE_AS_TELEGRAM_SENDS_IT,
    MY_PHONE_E164,
    TRANSLATIONS as SIGNUP_COPY,
    fire_conversation_timeout,
    make_bot,
    reach_phone_step,
)
from tests.telegram_bot.test_web_link_journey_dispatcher import (  # noqa: F401  (link_bot is a fixture)
    TOKEN, card_id, confirmed, link_bot,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def _allowed(_user_id):
    return True


async def build(monkeypatch, translations=SEEDED):
    """The harness on the real seed. One customer types many labels here, so the rate limiter answers
    "allowed", which is the production-normal answer, instead of reading the shared test Redis."""
    harness = await build_bot_harness(monkeypatch, translations=translations)
    monkeypatch.setattr(utils_module.rate_limiter, "allow_request", _allowed)
    return harness


@pytest.fixture
async def bot(monkeypatch):
    return await build(monkeypatch)


@pytest.fixture
def user(bot):
    return bot.updates()


async def send_menu(bot, *, silent=False, language="uz"):
    return await send_menu_keyboard(
        bot.application.bot, DEFAULT_CHAT_ID, DEFAULT_USER_ID,
        seeded("telegram.main_menu", language), language, silent=silent,
    )


# --- Task 14: one builder, one sender, one marker ------------------------------------------


async def test_the_bottom_keyboard_for_an_eligible_uzbek_customer(bot):
    assert tuple(UZ_LABELS) == MAIN_MENU_KEYS, "the layout follows the one key tuple"

    message = await send_menu(bot)

    assert message is not None
    [sent] = bot.telegram.of("sendMessage")
    assert sent.text == seeded("telegram.main_menu")
    assert sent.reply_markup == {
        "keyboard": UZ_ROWS,
        "is_persistent": True,
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }, "no input_field_placeholder: it would invite typed questions that get no reply"
    assert "disable_notification" not in sent.params


async def test_an_ineligible_customer_gets_no_aqua_club_button(bot):
    bot.database.loyalty_eligible = False

    await send_menu(bot)

    [sent] = bot.telegram.of("sendMessage")
    assert sent.reply_markup["keyboard"] == UZ_ROWS_WITHOUT_AQUA_CLUB
    assert stored_marker(bot) == {
        "labels": {key: label for key, label in UZ_LABELS.items() if key != "telegram.menu.loyalty"},
        "language": "uz",
        "loyalty": False,
    }


@pytest.mark.parametrize("unfinished", ["no row", "no phone"])
async def test_a_chat_that_has_not_finished_signup_gets_the_text_and_never_the_keyboard(bot, unfinished):
    """Spec anti-goal: a customer who has not finished signing up never gets the menu keyboard.
    send_menu_keyboard is the one gate, so no caller can forget it. A phone or location keyboard
    the flow left up still comes down."""
    if unfinished == "no row":
        bot.database.user = None
    else:
        bot.database.user["phone"] = None

    await send_menu(bot)

    [sent] = bot.telegram.of("sendMessage")
    assert (sent.text, sent.reply_markup) == (seeded("telegram.main_menu"), {"remove_keyboard": True})
    assert stored_marker(bot) is None


async def test_the_marker_records_the_labels_sent_once_telegram_accepted_them(bot):
    await send_menu(bot)

    assert stored_marker(bot) == {"labels": UZ_LABELS, "language": "uz", "loyalty": True}
    assert await installed_menu_labels(DEFAULT_USER_ID) == UZ_LABELS
    assert wanted_menu_labels("uz", show_loyalty=True) == UZ_LABELS


async def test_a_send_telegram_refuses_records_no_marker(bot):
    bot.telegram.fail("sendMessage", "Forbidden: bot was blocked by the user", 403)

    assert await send_menu(bot) is None
    assert stored_marker(bot) is None
    assert await installed_menu_labels(DEFAULT_USER_ID) == {}


async def test_a_silent_install_is_sent_without_a_notification(bot):
    await send_menu(bot, silent=True)

    [sent] = bot.telegram.of("sendMessage")
    assert sent.params["disable_notification"] is True


async def test_the_marker_survives_a_prompt_being_armed_and_disarmed(bot, user):
    await send_menu(bot)

    await bot.send(user.tap("report_issue_555"))
    armed = json.loads(bot.database.user["bot_state"])
    assert armed["awaiting_input"] == "support_message"
    assert armed[MENU_KEYBOARD_STATE_KEY]["labels"] == UZ_LABELS

    await bot.send(user.tap("support_cancel"))
    disarmed = json.loads(bot.database.user["bot_state"])
    assert "awaiting_input" not in disarmed
    assert disarmed[MENU_KEYBOARD_STATE_KEY]["labels"] == UZ_LABELS


async def test_an_edit_handed_the_bottom_keyboard_refuses_before_touching_telegram(bot):
    query = DummyCallbackQuery(data="anything")

    with pytest.raises(TypeError):
        await BaseHandler()._edit_or_replace_callback_message(
            query, "x", reply_markup=MenuKeyboards.bottom_menu("uz"),
        )

    query.edit_message_text.assert_not_awaited()
    query.message.delete.assert_not_awaited()
    query.message.reply_text.assert_not_awaited()


async def test_telegram_refuses_an_edit_that_carries_the_bottom_keyboard(bot):
    """What Telegram answers. With the transport mirroring it, any journey that edits with a reply
    keyboard (loyalty_menu, SimpleHandlers._send_response, menu.py) fails the way production would."""
    with pytest.raises(BadRequest, match="nline keyboard expected"):
        await bot.application.bot.edit_message_text(
            chat_id=DEFAULT_CHAT_ID, message_id=4242, text="x", reply_markup=MenuKeyboards.bottom_menu("uz"),
        )

    [refused] = bot.telegram.of("editMessageText")
    assert refused.refused == "Bad Request: inline keyboard expected"


# --- Task 15: where the keyboard is installed -----------------------------------------------

PROFILE = "/api/v1/auth/profile"
SEND_OTP = "/api/v1/auth/send-otp"
VERIFY_OTP = "/api/v1/auth/verify-phone"


async def test_start_for_a_registered_customer_installs_the_keyboard_under_the_menu_greeting(bot, user):
    await bot.send(user.command("start"))

    [carrier] = menu_sends(bot)
    assert (carrier.text, carrier.reply_markup["keyboard"]) == (seeded("telegram.main_menu"), UZ_ROWS)
    assert seeded("telegram.welcome") not in bot.telegram.texts(), "/start no longer says 'Registration complete'"
    assert stored_marker(bot) == {"labels": UZ_LABELS, "language": "uz", "loyalty": True}


@pytest.mark.parametrize("command", ["start", "menu"])
async def test_start_and_menu_reinstall_even_when_the_marker_says_it_is_installed(bot, user, command):
    """Review Focus 1: the customer cleared the chat or deleted the message that installed the keyboard.
    The marker cannot know that, so the two commands a stranded customer types always send the keyboard."""
    await bot.send(user.command("menu"))
    assert stored_marker(bot)["labels"] == UZ_LABELS
    bot.telegram.reset()

    await bot.send(user.command(command))

    [carrier] = menu_sends(bot)
    assert (carrier.text, carrier.reply_markup["keyboard"]) == (seeded("telegram.main_menu"), UZ_ROWS)


async def test_menu_from_an_unregistered_chat_gets_the_start_prompt_and_no_keyboard(bot, user):
    bot.database.user = None

    await bot.send(user.command("menu"))

    assert menu_sends(bot) == []
    refusal = bot.telegram.last_shown()
    assert refusal.text == (
        f"{seeded('telegram.registration_welcome')}\n\n{seeded('telegram.registration.start_command_prompt')}"
    )
    assert refusal.reply_markup == {"remove_keyboard": True}


async def test_menu_from_an_account_without_a_phone_points_to_start_and_shows_no_keyboard(bot, user):
    bot.database.user["phone"] = None

    await bot.send(user.command("menu"))

    assert menu_sends(bot) == []
    prompt = bot.telegram.last_shown()
    assert (prompt.text, prompt.reply_markup) == (
        seeded("telegram.registration.start_command_prompt"), {"remove_keyboard": True},
    )


async def test_a_verified_phone_code_installs_the_keyboard(bot, user):
    bot.backend.route("GET", PROFILE, lambda _c: {"data": {"phone": "+998978730111", "phone_verified": False}})
    bot.backend.route("POST", SEND_OTP, lambda _c: {"data": {"sent": True}})
    bot.backend.route("POST", VERIFY_OTP, lambda _c: {"data": {"verified": True}})
    await bot.send(user.tap("verify_phone_number"))
    bot.telegram.reset()

    await bot.send(user.text("123456"))

    [carrier] = menu_sends(bot)
    assert (carrier.text, carrier.params["parse_mode"]) == (seeded("telegram.bot.otp.success_message"), "Markdown")
    assert carrier.reply_markup["keyboard"] == UZ_ROWS


async def test_finishing_profile_phone_verification_installs_the_keyboard(bot, user):
    await bot.send(user.tap("add_phone_number"))
    await bot.send(user.contact("998978730111"))
    bot.telegram.reset()

    await bot.send(user.text("Kamola Karimova"))

    [carrier] = menu_sends(bot)
    assert (carrier.text, carrier.reply_markup["keyboard"]) == (seeded("telegram.profile_updated"), UZ_ROWS)


@pytest.mark.parametrize("how", ["contact", "typed"])
async def test_finishing_signup_installs_the_keyboard(make_bot, how):
    signup = await make_bot()
    user = signup.updates()
    await reach_phone_step(signup, user)

    def _verified_and_saved(_call):
        signup.database.row["phone"] = MY_PHONE_E164  # /verify-phone saves the number its code was sent to
        return {"data": {"verified": True}}

    signup.backend.route("POST", VERIFY_OTP, _verified_and_saved)
    if how == "contact":
        await signup.send(user.contact(MY_PHONE_AS_TELEGRAM_SENDS_IT))
    else:
        await signup.send(user.text(MY_PHONE_E164))
        await signup.send(user.text("123456"))

    carrier = signup.telegram.last_shown()
    assert carrier.text == SIGNUP_COPY[("uz", "telegram.registration_complete")]
    assert carrier.button_labels() == expected_labels("uz")


async def test_a_language_switch_sends_a_new_message_whose_keyboard_is_in_the_new_language(bot, user):
    await bot.send(user.tap("set_language_ru"))

    carrier = menu_sends(bot)[-1]
    assert seeded("telegram.language.confirmation_title", "ru") in carrier.text
    assert carrier.button_labels() == list(RU_LABELS.values())
    assert stored_marker(bot) == {"labels": RU_LABELS, "language": "ru", "loyalty": True}
    assert bot.telegram.of("editMessageText") == [], "the language card is left as it was"
    assert_no_edit_carries_a_reply_keyboard(bot)


async def test_an_account_without_a_phone_never_gets_the_keyboard_from_a_language_switch(bot, user):
    bot.database.user["phone"] = None

    await bot.send(user.tap("set_language_ru"))

    [confirmation] = [
        c for c in bot.telegram.of("sendMessage")
        if seeded("telegram.language.confirmation_title", "ru") in c.text
    ]
    assert confirmation.reply_markup == {"remove_keyboard": True}
    assert menu_sends(bot) == [] and stored_marker(bot) is None


async def test_an_old_language_label_still_opens_its_screen_after_a_language_switch(monkeypatch):
    """Spec Tests list: the customer switched to ru, but a uz label is still on screen (an old
    message, another device). It opens its screen in the NEW language and is never filed to support.

    `customer_speaking` (tests/telegram_bot/test_language_and_i18n_journeys.py) keeps the
    `UPDATE users SET preferred_language` the switch writes; the default FakeDatabase drops it."""
    from tests.telegram_bot.test_language_and_i18n_journeys import customer_speaking

    bot = await build_bot_harness(monkeypatch, translations=SEEDED, database=customer_speaking("uz"))
    monkeypatch.setattr(utils_module.rate_limiter, "allow_request", _allowed)
    user = bot.updates()
    await bot.send(user.tap("set_language_ru"))
    assert bot.database.user["preferred_language"] == "ru"
    bot.telegram.reset(); bot.backend.calls.clear()

    await bot.send(user.text(UZ_LABELS["telegram.menu.support"]))

    assert bot.telegram.last_shown().text.startswith(seeded("telegram.support.menu_intro", "ru"))
    assert not [c for c in bot.backend.calls if c.endpoint == "/api/v1/support/messages"]


async def test_menu_right_after_signup_reads_the_live_phone_not_the_cached_phoneless_row(bot, user):
    """The cache `user_middleware` keeps for minutes still holds the phoneless row that signup began
    with; the phone was saved since. /menu must install the keyboard, never take it down."""
    bot.database.user["phone"] = None
    await bot.send(user.command("menu"))          # primes the real cache with the phoneless row
    assert menu_sends(bot) == []
    bot.database.user["phone"] = "+998978730111"  # signup / OTP saved the number
    bot.telegram.reset()

    await bot.send(user.command("menu"))

    [carrier] = menu_sends(bot)
    assert (carrier.text, carrier.reply_markup["keyboard"]) == (seeded("telegram.main_menu"), UZ_ROWS)
    assert not [c for c in bot.telegram.shown if c.reply_markup == {"remove_keyboard": True}]


# --- Task 16: every exit puts the keyboard back; edits carry no menu ------------------------

LEGACY_MENU_CALLBACKS = {
    "menu_products", "menu_orders", "cart_view", "menu_subscriptions",
    "menu_loyalty", "menu_profile", "menu_support", "menu_language",
}
PIN = (41.32354, 69.241036)


@pytest.mark.parametrize("conversation,copy_key", [
    ("phone_verification", "telegram.phone.verification_flow_timed_out"),
    ("subscription_creation", "telegram.subscription.flow_timed_out"),
    ("item_management", "telegram.subscription.flow_timed_out"),
    ("update_item", "telegram.subscription.flow_timed_out"),
    ("address_conversation", "telegram.address.flow_timed_out"),
])
async def test_every_timeout_puts_the_keyboard_back(bot, user, conversation, copy_key):
    await fire_conversation_timeout(bot, conversation, user.tap("a_button_from_the_expired_step"))

    [carrier] = menu_sends(bot)
    assert (carrier.text, carrier.reply_markup["keyboard"]) == (seeded(copy_key), UZ_ROWS)


async def test_cancelling_the_address_form_from_its_keyboard_puts_the_menu_back_in_one_message(bot, user):
    await bot.send(user.tap("add_new_address"))
    bot.telegram.reset()

    await bot.send(user.text(seeded("telegram.cancel")))

    assert bot.conversation_state("address_conversation") is None
    assert bot.telegram.texts() == [seeded("telegram.action_cancelled")]
    [carrier] = menu_sends(bot)
    assert carrier.reply_markup["keyboard"] == UZ_ROWS


@pytest.mark.parametrize("step,copy_key", [
    ("pin", "telegram.address.location_received"),
    ("manual", "telegram.address.manual_entry_started"),
])
async def test_leaving_the_location_keyboard_puts_the_menu_back(bot, user, step, copy_key):
    await bot.send(user.tap("add_new_address"))
    bot.telegram.reset()

    if step == "pin":
        await bot.send(user.location(*PIN))
    else:
        await bot.send(user.text(seeded("telegram.address.enter_manually_button")))

    [carrier] = menu_sends(bot)
    assert (carrier.text, carrier.reply_markup["keyboard"]) == (seeded(copy_key), UZ_ROWS)
    assert not [c for c in bot.telegram.of("sendMessage") if c.reply_markup.get("remove_keyboard")]


async def test_an_unexpected_error_at_the_pin_step_still_puts_the_menu_back(bot, user):
    """A bug, not a refusal: a failed geocode is an APIResponse and the step carries on. An exception
    from inside the step ends the flow, and the location keyboard must not outlive it."""
    def _raises(_call):
        raise RuntimeError("unexpected")

    bot.backend.route("POST", "/api/v1/addresses/reverse-geocode", _raises)
    await bot.send(user.tap("add_new_address"))
    bot.telegram.reset()

    await bot.send(user.location(*PIN))

    assert bot.conversation_state("address_conversation") is None
    [carrier] = menu_sends(bot)
    assert (carrier.text, carrier.reply_markup["keyboard"]) == (seeded("telegram.error_occurred"), UZ_ROWS)


async def test_an_error_after_the_shared_contact_still_puts_the_menu_back(bot, user, monkeypatch):
    """profile.py's phone_verify_contact_received except branch used to end the flow in silence,
    with the one-time phone keyboard already gone and no menu in its place."""
    await bot.send(user.tap("add_phone_number"))
    bot.telegram.reset()
    write = database_module.db_manager.execute

    async def _the_phone_write_fails(query, *args):
        if "phone_verified_at" in query:
            raise RuntimeError("connection reset by peer")
        return await write(query, *args)

    monkeypatch.setattr(database_module.db_manager, "execute", _the_phone_write_fails)

    await bot.send(user.contact("998978730111"))

    assert bot.conversation_state("phone_verification") is None
    [carrier] = menu_sends(bot)
    assert (carrier.text, carrier.reply_markup["keyboard"]) == (seeded("telegram.error_occurred"), UZ_ROWS)


@pytest.mark.parametrize("last_step", ["typed", "skipped"])
async def test_finishing_a_pinned_address_ends_on_the_keyboard_or_on_a_bare_edit(bot, user, last_step):
    await bot.send(user.tap("add_new_address"))
    await bot.send(user.location(*PIN))
    await bot.send(user.tap("addr_title_home"))
    await bot.send(user.tap("skip_apartment"))
    await bot.send(user.tap("skip_floor"))
    bot.telegram.reset()

    if last_step == "typed":
        await bot.send(user.text("Domofon 45"))
        [carrier] = menu_sends(bot)
        assert (carrier.text, carrier.reply_markup["keyboard"]) == (
            seeded("telegram.address.saved_successfully"), UZ_ROWS,
        )
    else:
        await bot.send(user.tap("skip_delivery_instructions"))
        saved = bot.telegram.last_shown()
        assert (saved.method, saved.text, saved.reply_markup) == (
            "editMessageText", seeded("telegram.address.saved_successfully"), {},
        )
        assert menu_sends(bot) == []
    assert bot.conversation_state("address_conversation") is None


@pytest.mark.parametrize("tap", ["back_to_main", "cancel_order", "menu_orders"])
async def test_screens_that_used_to_end_on_the_inline_menu_now_carry_none(bot, user, tap):
    await bot.send(user.tap(tap))

    edits = bot.telegram.of("editMessageText", "editMessageReplyMarkup")
    assert edits, f"{tap!r} edited nothing"
    for edit in edits:
        assert not LEGACY_MENU_CALLBACKS & set(edit.callback_data()), (tap, edit.callback_data())
    assert_no_edit_carries_a_reply_keyboard(bot)


async def test_my_orders_with_none_answers_a_typed_tap_with_the_keyboard(bot, user):
    await bot.send(user.text(seeded("telegram.menu.orders")))

    [carrier] = menu_sends(bot)
    assert (carrier.text, carrier.reply_markup["keyboard"]) == (seeded("telegram.orders.no_orders"), UZ_ROWS)


@pytest.mark.parametrize("how", ["inline", "typed"])
async def test_an_ineligible_customer_tapping_aqua_club_gets_the_notice_and_a_keyboard_without_it(bot, user, how):
    bot.database.loyalty_eligible = False

    await bot.send(user.tap("menu_loyalty") if how == "inline" else user.text("🎁 Aqua Club"))

    carrier = menu_sends(bot)[-1]
    assert (carrier.text, carrier.reply_markup["keyboard"]) == (
        seeded("telegram.loyalty.not_available"), UZ_ROWS_WITHOUT_AQUA_CLUB,
    )
    assert_no_edit_carries_a_reply_keyboard(bot)


# --- Task 16 fix round 1: the phone form leaves the address form's own labels alone ---------
# Both forms can be open at once: with the phone form open, an older inline "Add address" button
# (subscriptions, checkout) still opens the address form and its location keyboard. The phone
# form is registered first in group -2, and PTB runs one handler per group, so any label it
# claimed would never reach the address form.

TYPED_NUMBER = "+998935554433"
PHONE_FORM_STEPS = {"phone": PHONE_VERIFY_PHONE, "code": PHONE_VERIFY_OTP, "name": PHONE_VERIFY_NAME}


async def open_both_forms(bot, user, phone_step):
    """The phone form at `phone_step`, then the address form opened over it from an inline button."""
    await bot.send(user.tap("add_phone_number"))
    if phone_step == "code":
        await bot.send(user.text(TYPED_NUMBER))
    elif phone_step == "name":
        await bot.send(user.contact("998978730111"))
    assert bot.conversation_state("phone_verification") == PHONE_FORM_STEPS[phone_step]
    await bot.send(user.tap("add_new_address"))
    assert bot.conversation_state("address_conversation") == ADDRESS_LOCATION
    bot.telegram.reset()
    bot.backend.calls.clear()


def phone_form_calls(bot):
    """What the phone form would send the backend if it took the label as its own answer."""
    return [
        (c.method, c.endpoint, c.data) for c in bot.backend.calls
        if (c.method, c.endpoint) in {("PUT", PROFILE), ("POST", SEND_OTP), ("POST", VERIFY_OTP)}
    ]


@pytest.mark.parametrize("phone_step", ["phone", "code", "name"])
async def test_enter_manually_reaches_the_address_form_while_the_phone_form_is_open(bot, user, phone_step):
    await open_both_forms(bot, user, phone_step)

    await bot.send(user.text(seeded("telegram.address.enter_manually_button")))

    assert phone_form_calls(bot) == [], "the label was taken as a number, a code or a name"
    assert bot.conversation_state("address_conversation") == ADDRESS_REGION
    assert bot.conversation_state("phone_verification") == PHONE_FORM_STEPS[phone_step]
    [carrier] = menu_sends(bot)
    assert carrier.text == seeded("telegram.address.manual_entry_started")


@pytest.mark.parametrize("phone_step", ["code", "name"])
async def test_the_location_keyboards_cancel_cancels_the_address_form_not_the_phone_form(bot, user, phone_step):
    await open_both_forms(bot, user, phone_step)

    await bot.send(user.text(seeded("telegram.cancel")))

    assert bot.conversation_state("address_conversation") is None
    [carrier] = menu_sends(bot)
    assert (carrier.text, carrier.reply_markup["keyboard"]) == (seeded("telegram.action_cancelled"), UZ_ROWS)
    assert seeded("telegram.address.flow_timed_out") not in bot.telegram.texts(), (
        "the address form was reported as timed out while it was still open"
    )
    assert phone_form_calls(bot) == []
    assert bot.conversation_state("phone_verification") == PHONE_FORM_STEPS[phone_step]

    if phone_step == "code":
        # The code prompt is untouched: the code read off the SMS still verifies, in the flow.
        bot.backend.route("POST", VERIFY_OTP, lambda _c: {"data": {"verified": True}})
        await bot.send(user.text("123456"))
        assert bot.conversation_state("phone_verification") == PHONE_VERIFY_NAME
        assert bot.telegram.texts()[-1] == seeded("telegram.enter_name")


# --- Task 17: installing for customers who already have the bot ----------------------------


async def test_the_first_inline_tap_installs_the_keyboard_once_silently_before_the_screen(bot, user):
    await bot.send(user.tap("faq"))

    visible = [c for c in bot.telegram.calls if c.method in ("sendMessage", "editMessageText")]
    assert [c.method for c in visible] == ["sendMessage", "editMessageText"]
    install, screen = visible
    assert (install.text, install.reply_markup["keyboard"]) == (seeded("telegram.main_menu"), UZ_ROWS)
    assert install.params["disable_notification"] is True
    assert screen.text == seeded("telegram.support.faq_coming_soon")
    assert stored_marker(bot)["labels"] == UZ_LABELS

    bot.telegram.reset()
    await bot.send(user.tap("contact_support"))
    assert menu_sends(bot) == [], "a current marker installs nothing"


async def test_typed_text_never_installs_the_keyboard(bot, user):
    await bot.send(user.text("Suvim qachon keladi?"))

    assert menu_sends(bot) == []
    assert stored_marker(bot) is None


@pytest.mark.parametrize("unfinished", ["no row", "no phone"])
async def test_a_chat_that_has_not_finished_signup_never_gets_the_keyboard_from_a_tap(bot, user, unfinished):
    if unfinished == "no row":
        bot.database.user = None
    else:
        bot.database.user["phone"] = None

    await bot.send(user.tap("faq"))

    assert menu_sends(bot) == []
    assert seeded("telegram.main_menu") not in bot.telegram.texts(), (
        "the installer sent its message anyway, with the keyboard taken down, and would on every tap"
    )


async def test_a_tap_while_signup_waits_for_the_code_never_installs_the_keyboard(link_bot):
    """Confirm linked an account whose number is on file, so the row has a phone; registration is
    still open at REGISTER_OTP and owns this customer until the code is in."""
    bot = await link_bot(confirm=confirmed(phone_status="unverified"))
    user = bot.updates()
    await bot.send(user.command(f"/start web_{TOKEN}"))
    await bot.send(user.tap("weblink_yes", card_id(bot)))
    assert bot.conversation_state("registration") == REGISTER_OTP
    bot.telegram.reset()

    await bot.send(user.tap("faq"))

    assert menu_sends(bot) == []
    assert bot.conversation_state("registration") == REGISTER_OTP


async def test_a_language_changed_elsewhere_reinstalls_on_the_next_tap(bot, user):
    await bot.send(user.tap("faq"))
    assert stored_marker(bot)["language"] == "uz"
    bot.database.user["preferred_language"] = "ru"  # changed on the website
    bot.telegram.reset()

    await bot.send(user.tap("contact_support"))

    [install] = menu_sends(bot)
    assert (install.text, install.button_labels()) == (seeded("telegram.main_menu", "ru"), list(RU_LABELS.values()))


async def test_the_install_never_covers_the_phone_keyboard(bot, user):
    await bot.send(user.tap("add_phone_number"))

    keyboards = [c for c in bot.telegram.of("sendMessage") if "keyboard" in c.reply_markup]
    assert [c.button_labels() for c in keyboards] == [
        list(UZ_LABELS.values()),
        [seeded("telegram.profile.share_phone"), seeded("telegram.cancel")],
    ]
    assert keyboards[-1].reply_markup["keyboard"][0][0]["request_contact"] is True


async def test_the_install_never_covers_the_location_keyboard(bot, user):
    await bot.send(user.tap("add_new_address"))

    keyboards = [c for c in bot.telegram.of("sendMessage") if "keyboard" in c.reply_markup]
    assert keyboards[0].button_labels() == list(UZ_LABELS.values())
    assert keyboards[-1].reply_markup["keyboard"][0][0].get("request_location") is True


async def test_a_retitled_button_still_leaves_the_address_form_from_the_street_step(monkeypatch):
    """Review Focus 4: the owner retitles Help while a customer, with the old label still on their
    keyboard, is at the street step. The old label leaves the form and opens Help. It is never the street."""
    kept = MAIN_MENU_KEYS + ("telegram.main_menu", "telegram.support.menu_intro", "telegram.help.command_hint")
    table = {**MANUAL_ENTRY_COPY, **{k: v for k, v in SEEDED.items() if k[1] in kept}}
    bot = await build(monkeypatch, translations=table)
    user = bot.updates()

    await walk_to_the_street_prompt(bot, user)  # the manual-entry step installed the keyboard
    old_label = stored_marker(bot)["labels"]["telegram.menu.support"]
    assert old_label == "🆘 Yordam"
    _install_translations(monkeypatch, {
        **table, **{(lang, "telegram.menu.support"): "🆘 Yordam markazi" for lang in ("uz", "ru", "en")},
    })
    bot.backend.calls.clear()
    bot.telegram.reset()

    await bot.send(user.text(old_label))

    assert bot.conversation_state("address_conversation") is None
    assert seeded("telegram.support.menu_intro") in bot.telegram.last_shown().text
    assert not [c for c in bot.backend.calls if old_label in json.dumps(c.data or {}, ensure_ascii=False)]
    assert "address_draft" not in json.loads(bot.database.user["bot_state"])


# --- Task 18: Back goes to the real parent; one Help screen ---------------------------------


@pytest.mark.parametrize("tap,parent", [
    ("loyalty_history", "menu_loyalty"),
    ("my_bottles", "menu_profile"),
    ("menu_language", "menu_profile"),
])
async def test_back_on_a_second_level_screen_goes_to_its_real_parent(bot, user, tap, parent):
    await bot.send(user.tap(tap))

    screen = bot.telegram.last_shown()
    assert screen.callback_data()[-1] == parent
    assert "back_to_main" not in screen.callback_data()


async def test_back_from_points_history_opens_the_aqua_club_screen(bot, user):
    await bot.send(user.tap("loyalty_history"))
    await bot.send(user.tap("menu_loyalty"))

    screen = bot.telegram.last_shown()
    assert screen.text.startswith(seeded("telegram.menu.loyalty"))
    assert screen.callback_data()[:3] == ["loyalty_history", "loyalty_rewards", "loyalty_referral"]
    assert "back_to_main" not in screen.callback_data()


async def test_my_profile_offers_language_and_no_back_to_main(bot, user):
    bot.backend.route("GET", PROFILE, lambda _c: {"data": {"first_name": "Kamola", "phone": "+998978730111"}})

    await bot.send(user.tap("menu_profile"))

    screen = bot.telegram.last_shown()
    assert (seeded("telegram.menu.language"), "menu_language") in zip(screen.button_labels(), screen.callback_data())
    assert "back_to_main" not in screen.callback_data()


async def test_back_to_main_on_an_old_message_only_takes_that_bubbles_buttons_away(bot, user):
    await menu_already_installed(bot)

    await bot.send(user.tap("back_to_main", message_id=4242))

    calls = [c for c in bot.telegram.calls if c.method != "answerCallbackQuery"]
    assert [c.method for c in calls] == ["editMessageReplyMarkup"]
    assert calls[0].params["message_id"] == 4242 and calls[0].callback_data() == []
    assert bot.telegram.visible_answers, "the tap is acknowledged"


async def test_help_and_the_help_button_show_one_support_screen(bot, user):
    await bot.send(user.command("help"))
    via_command = bot.telegram.last_shown()
    bot.telegram.reset()
    await bot.send(user.text(seeded("telegram.menu.support")))
    via_button = bot.telegram.last_shown()

    expected = f"{seeded('telegram.support.menu_intro')}\n\n{seeded('telegram.help.command_hint')}"
    assert via_command.text == via_button.text == expected
    assert via_command.callback_data() == via_button.callback_data() == []


async def test_help_from_an_unregistered_chat_asks_them_to_start(bot, user):
    bot.database.user = None

    await bot.send(user.command("help"))

    assert bot.telegram.last_shown().text == (
        f"{seeded('telegram.registration_welcome')}\n\n{seeded('telegram.registration.start_command_prompt')}"
    )


# --- Final fix I1: leaving a share-location or share-contact step puts the menu back -------------
# The flow exit (`flow_exit.leave_all_flows`) ends the step, and /start and /menu install the
# keyboard themselves. Every other exit gesture used to leave the location or contact keyboard up,
# with a Share button that no longer answered anything: its pin was filed silently to the Support
# Inbox, its contact was ignored. Spec §1: every exit from a temporary-keyboard step reinstalls it.

EXITS_THAT_DRAW_NO_KEYBOARD = [
    pytest.param(lambda u: u.tap("menu_profile"), id="Back-to-My-Profile"),
    pytest.param(lambda u: u.tap("menu_loyalty"), id="Back-to-Aqua-Club"),
    pytest.param(lambda u: u.tap("menu_products"), id="Continue-shopping"),
    pytest.param(lambda u: u.tap("cart_view"), id="cart_view"),
    pytest.param(lambda u: u.tap("back_to_main"), id="back_to_main"),
    pytest.param(lambda u: u.command("help"), id="/help"),
    pytest.param(lambda u: u.command("language"), id="/language"),
    pytest.param(lambda u: u.text(UZ_LABELS["telegram.menu.profile"]), id="typed-label"),
    pytest.param(lambda u: u.text(RU_LABELS["telegram.menu.support"]), id="typed-label-in-another-language"),
]


def reply_keyboards(bot):
    """Every sendMessage that changed the customer's reply keyboard, in order."""
    return [c for c in bot.telegram.of("sendMessage") if {"keyboard", "remove_keyboard"} & set(c.reply_markup)]


def share_button(call):
    return call.reply_markup["keyboard"][0][0]


async def at_the_location_step(bot, user):
    await menu_already_installed(bot)
    await bot.send(user.tap("add_new_address"))
    assert bot.conversation_state("address_conversation") == ADDRESS_LOCATION
    assert share_button(reply_keyboards(bot)[-1]).get("request_location") is True


async def at_the_contact_step(bot, user):
    await menu_already_installed(bot)
    await bot.send(user.tap("add_phone_number"))
    assert bot.conversation_state("phone_verification") == PHONE_VERIFY_PHONE
    assert share_button(reply_keyboards(bot)[-1]).get("request_contact") is True


async def at_the_checkout_pin_prompt(bot, user):
    """No saved address: Checkout asks for a pin on a location keyboard and starts no form."""
    await menu_already_installed(bot)
    await bot.send(user.tap("checkout"))
    assert bot.conversation_state("address_conversation") is None
    assert share_button(reply_keyboards(bot)[-1]).get("request_location") is True


TEMPORARY_KEYBOARD_STEPS = {
    "address-location": at_the_location_step,
    "profile-phone": at_the_contact_step,
    "checkout-pin": at_the_checkout_pin_prompt,
}


def assert_the_menu_is_back_silently(bot):
    """Exactly one install, silent, and nothing after it put a share keyboard back up."""
    [install] = menu_sends(bot)
    assert (install.text, install.reply_markup["keyboard"]) == (seeded("telegram.main_menu"), UZ_ROWS)
    assert install.params["disable_notification"] is True
    assert reply_keyboards(bot) == [install], "the keyboard left on screen must be the bottom menu"


@pytest.mark.parametrize("gesture", EXITS_THAT_DRAW_NO_KEYBOARD)
@pytest.mark.parametrize("step", TEMPORARY_KEYBOARD_STEPS)
async def test_leaving_a_share_step_by_any_other_exit_puts_the_menu_back(bot, user, step, gesture):
    await TEMPORARY_KEYBOARD_STEPS[step](bot, user)
    bot.telegram.reset()

    await bot.send(gesture(user))

    assert bot.conversation_state("address_conversation") is None
    assert bot.conversation_state("phone_verification") is None
    assert_the_menu_is_back_silently(bot)


@pytest.mark.parametrize("command", ["start", "menu"])
@pytest.mark.parametrize("step", TEMPORARY_KEYBOARD_STEPS)
async def test_start_and_menu_at_a_share_step_install_the_menu_once(bot, user, step, command):
    """The two commands install the keyboard themselves: the exit adds no second, silent copy."""
    await TEMPORARY_KEYBOARD_STEPS[step](bot, user)
    bot.telegram.reset()

    await bot.send(user.command(command))

    [install] = menu_sends(bot)
    assert (install.text, install.reply_markup["keyboard"]) == (seeded("telegram.main_menu"), UZ_ROWS)
    assert "disable_notification" not in install.params, "the command's own install, not the exit's"
    assert reply_keyboards(bot) == [install]


async def test_after_a_restart_the_saved_pin_prompt_still_puts_the_menu_back(bot, user):
    """A deploy empties `user_data`; the location keyboard is client-side and stays, and so does the
    pin prompt's durable twin in bot_state, which is what the exit reads then."""
    await at_the_checkout_pin_prompt(bot, user)
    bot.application.user_data[DEFAULT_USER_ID].clear()
    assert "awaiting_location_at" in json.loads(bot.database.user["bot_state"])
    bot.telegram.reset()

    await bot.send(user.command("help"))

    assert_the_menu_is_back_silently(bot)
    assert "awaiting_location_at" not in json.loads(bot.database.user["bot_state"])


async def test_an_exit_after_the_menu_already_replaced_the_location_keyboard_sends_no_second_menu(bot, user):
    """Enter manually put the menu back (the pin prompt stays armed for a pin). Leaving the form from
    the region step draws only the destination."""
    await at_the_location_step(bot, user)
    await bot.send(user.text(seeded("telegram.address.enter_manually_button")))
    assert bot.conversation_state("address_conversation") == ADDRESS_REGION
    bot.telegram.reset()

    await bot.send(user.command("help"))

    assert bot.conversation_state("address_conversation") is None
    assert menu_sends(bot) == []


async def test_an_exit_during_signups_share_contact_step_installs_nothing(make_bot):
    """Signup is never ended by an exit, and a customer without a phone never gets the menu."""
    bot = await make_bot()
    user = bot.updates()
    assert await reach_phone_step(bot, user) == PHONE
    bot.telegram.reset()

    await bot.send(user.command("help"))

    assert bot.conversation_state("registration") == PHONE
    assert menu_sends(bot) == []
    assert [c for c in bot.telegram.of("sendMessage") if c.params.get("disable_notification")] == []
