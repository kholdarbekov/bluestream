"""A web-only customer connects this Telegram from /my-account: the bot side, through the REAL dispatcher.

Spec 2026-10-19 D5, D8, D12, D15. The website hands the customer
`t.me/<bot>?start=web_<token>`; everything after that tap is the customer bot's:

* the `web_` payload is parsed BEFORE the existing-user lookup and before anything that could
  create a row (a stub row from the language card would block the next link), and never reaches
  the referral capture;
* the bot asks the backend to preview the link and shows ONE confirm card (option A), in HTML,
  with the masked label escaped as data (RF3);
* nothing is connected until Yes; Cancel, the 300 s timeout, a stale button after a restart and a
  plain /start all forget the link;
* a refused or failed link always says so (R6, D15f), never silence and never the language card;
* after Yes: the greeting in the website account's language (R5), or first the SMS check of the
  number on file (D12), or today's contact-share step for an account with no phone, whose number
  another account holds gets the usual merge offer and its own success message (R8).

Only Telegram, the backend and the bot's own SQL are faked (tests/telegram_bot/ptb_harness.py).
The backend doubles answer with the shapes the real routes publish
(tests/integration/test_customer_telegram_link_api.py pins those): 200 `{"data": {...}}`, and a
refusal with its `error_code` at the TOP level of the body.
"""

import pytest
from telegram.ext import ConversationHandler

from handlers.conversation_states import LINK_ACCOUNT_CONFIRM, LINK_ACCOUNT_OTP, WEB_LINK_CONFIRM
from handlers.profile import (
    PHONE,
    REGISTER_OTP,
    SELECT_LANGUAGE,
    WEB_LINK_GREETING_KEY,
    WEB_LINK_PENDING_KEY,
)
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from shared.i18n_rendering import render_translation

from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, _install_translations, backend_failure
from tests.telegram_bot.test_checkout_journey_dispatcher import expire_dedup_window
from tests.telegram_bot.test_registration_journey_dispatcher import (  # noqa: F401  (make_bot is a fixture)
    CHECK_PHONE,
    LINK_SEND_OTP,
    LINK_VERIFY,
    MY_PHONE_AS_TELEGRAM_SENDS_IT,
    MY_PHONE_E164,
    REGISTER,
    SEND_OTP,
    TRANSLATIONS,
    VERIFY_PHONE,
    fire_conversation_timeout,
    has_contact_button,
    make_bot,
    payloads,
    phone_belongs_to,
    posts,
    reach_phone_step,
    state,
    texts,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


PREVIEW = "/api/v1/bot/web-link/preview"
CONFIRM = "/api/v1/bot/web-link/confirm"
# Both create a users row for an unknown Telegram id (auth_service telegram-register / the
# telegram-login auto-create), so neither may run before the link is redeemed (the spec's first anti-goal).
TELEGRAM_LOGIN = "/api/v1/auth/telegram-login"

LANGS = ("en", "uz", "ru")
# The seeded copy this flow renders, served from the real seed so a test reads what a customer reads.
BOT_KEYS = (
    "telegram.web_link.confirm_prompt",
    "telegram.web_link.yes_button",
    "telegram.web_link.connected",
    "telegram.web_link.expired",
    "telegram.web_link.has_account",
    "telegram.web_link.cancelled",
    "telegram.web_link.failed",
    "telegram.web_link.confirm_number_prompt",
    "telegram.support.menu_intro",
    "telegram.cancel",
    "telegram.common.user_fallback",
    "telegram.registration.share_contact_prompt",
    "telegram.phone.verification_sms_send_failed",
)

# secrets.token_urlsafe(24) is 32 characters (spec D2).
TOKEN = "k3Jx_9Qw-Lm2Np4Rs6Tu8Vw0Xy1Ab3Cd"

PREVIEW_OK = {"data": {"account_label": "T*** U*** · +998***4567", "language": "ru"}}

# make_bot's registered-row shape, with a number on file and Uzbek saved as the bot language.
ROW = {
    "id": 4242,
    "telegram_id": str(DEFAULT_USER_ID),
    "first_name": "Kamola",
    "phone": "+998901112233",
    "preferred_language": "uz",
    "role": "customer",
    "status": "active",
    "bot_state": "{}",
    "user_type": "individual",
}


def confirmed(phone_status="verified", phone="+998901112233"):
    return {
        "data": {
            "tokens": {"access_token": "link-access", "refresh_token": "link-refresh", "expires_in": 3600},
            "language": "ru",
            "greeting_name": "Zarina",
            "phone_status": phone_status,
            "phone": phone,
        }
    }


def refused(code, status=409):
    """A refusal as `@handle_exceptions` sends it: `error_code` at the top level of the body."""
    return backend_failure(code, status, data={"error_code": code, "message": code})


def copy(key, language="ru", **values):
    """The seeded copy, rendered by production's own renderer (C8)."""
    return render_translation(key, BACKEND_TRANSLATIONS[key][language], (), values)


def no_language_card(bot):
    return not any(c.startswith("set_language_") for s in bot.telegram.shown for c in s.callback_data())


def created_no_account(bot):
    return not [c for c in bot.backend.calls if c.endpoint in (REGISTER, TELEGRAM_LOGIN)]


def card_id(bot):
    """The message id of the confirm card just shown: the bot's last message.

    A real tap carries the message it was made on, and only the newest card answers (M-F1).
    """
    return bot.telegram._next_message_id


@pytest.fixture
async def link_bot(make_bot, monkeypatch):
    """The registration bot with the link routes answered and the link copy served from the seed."""

    async def _make(*, row=None, preview=None, confirm=None):
        bot = await make_bot(row=row)
        _install_translations(monkeypatch, {
            **TRANSLATIONS,
            **{(lang, key): BACKEND_TRANSLATIONS[key][lang] for key in BOT_KEYS for lang in LANGS},
        })
        bot.backend.route("POST", PREVIEW, lambda _call: preview or PREVIEW_OK)
        bot.backend.route("POST", CONFIRM, lambda _call: confirm or confirmed())
        return bot

    return _make


# ===========================================================================
# The confirm card, Yes and Cancel (R4, R5, RF3)
# ===========================================================================


@pytest.mark.parametrize("label,shown", [("T*** U*** · +998***4567", "T*** U*** · +998***4567"),
                                         ("_*** · [x] <i>&", "_*** · [x] &lt;i&gt;&amp;")])          # RF3
async def test_a_web_link_shows_one_escaped_confirm_card_and_creates_no_account(link_bot, label, shown):
    bot = await link_bot(preview={"data": {"account_label": label, "language": "ru"}}); user = bot.updates()
    await bot.send(user.command(f"/start web_{TOKEN}"))
    [call] = posts(bot, PREVIEW)
    assert (call.data, call.sign) == ({"token": TOKEN, "telegram_id": DEFAULT_USER_ID}, True)
    card = bot.telegram.last_shown()
    assert (card.text, card.params["parse_mode"]) == (copy("telegram.web_link.confirm_prompt", account=shown), "HTML")
    assert (card.callback_data(), card.button_labels()) == (
        ["weblink_yes", "weblink_no"], [copy("telegram.web_link.yes_button"), copy("telegram.cancel")]
    )
    assert state(bot) == WEB_LINK_CONFIRM and posts(bot, REGISTER) == [] and no_language_card(bot)
    assert "referral_code" not in bot.application.user_data[user.user_id]
    assert created_no_account(bot) and len(bot.telegram.shown) == 1


async def test_yes_connects_stores_the_tokens_and_greets_in_the_account_language(link_bot):
    bot = await link_bot(); user = bot.updates()
    await bot.send(user.command(f"/start web_{TOKEN}")); await bot.send(user.tap("weblink_yes", card_id(bot)))
    [call] = posts(bot, CONFIRM)
    assert (call.data, call.sign) == ({"token": TOKEN, "telegram_id": DEFAULT_USER_ID, "username": "kamola_test",
                                       "first_name": "Kamola", "last_name": None}, True)
    assert bot.tokens.stored == [{"user_id": DEFAULT_USER_ID, "access_token": "link-access",
                                  "refresh_token": "link-refresh", "expires_in": 3600}]
    shown = bot.telegram.last_shown()
    assert shown.text == copy("telegram.web_link.connected", name="Zarina") and "menu_support" in shown.callback_data()
    assert bot.telegram.of("editMessageReplyMarkup") and state(bot) is None and posts(bot, REGISTER) == []
    assert WEB_LINK_PENDING_KEY not in bot.application.user_data[user.user_id]
    assert created_no_account(bot)


async def test_a_connected_customer_with_no_name_is_greeted_with_the_seeded_fallback(link_bot):
    reply = confirmed(); reply["data"]["greeting_name"] = None
    bot = await link_bot(confirm=reply); user = bot.updates()
    await bot.send(user.command(f"/start web_{TOKEN}")); await bot.send(user.tap("weblink_yes", card_id(bot)))
    assert texts(bot)[-1] == copy("telegram.web_link.connected", name=copy("telegram.common.user_fallback"))


async def test_cancel_connects_nothing(link_bot):
    bot = await link_bot(); user = bot.updates()
    await bot.send(user.command(f"/start web_{TOKEN}")); await bot.send(user.tap("weblink_no", card_id(bot)))
    assert posts(bot, CONFIRM) == [] and texts(bot)[-1] == copy("telegram.web_link.cancelled")
    assert state(bot) is None and posts(bot, REGISTER) == [] and created_no_account(bot)
    assert bot.telegram.of("editMessageReplyMarkup") and no_language_card(bot)
    assert WEB_LINK_PENDING_KEY not in bot.application.user_data[user.user_id]


# ===========================================================================
# When the link cannot connect (R6, D15a, D15f, RF2, RF4)
# ===========================================================================


@pytest.mark.parametrize("code,status", [("WEB_LINK_EXPIRED", 400), ("WEB_LINK_NOT_AVAILABLE", 409)])
async def test_a_dead_link_from_an_unknown_telegram_says_so_and_shows_no_language_card(link_bot, code, status):
    bot = await link_bot(preview=refused(code, status)); user = bot.updates()
    await bot.send(user.command(f"/start web_{TOKEN}"))
    assert texts(bot)[-1] == copy("telegram.web_link.expired", "uz")
    assert state(bot) is None and posts(bot, REGISTER) == [] and no_language_card(bot) and created_no_account(bot)


async def test_a_dead_link_from_a_known_telegram_is_a_plain_start(link_bot):                  # D15a
    bot = await link_bot(row=dict(ROW), preview=refused("WEB_LINK_EXPIRED", 400)); user = bot.updates()
    await bot.send(user.command(f"/start web_{TOKEN}"))
    assert texts(bot)[-1] == TRANSLATIONS[("uz", "telegram.welcome")]
    assert "menu_support" in bot.telegram.last_shown().callback_data() and state(bot) is None


@pytest.mark.parametrize("phone", ["+998901112233", None])
async def test_a_telegram_with_its_own_account_goes_to_the_chat_and_the_flow_ends(link_bot, phone):   # R6, D15a
    bot = await link_bot(row={**ROW, "phone": phone}, preview=refused("TELEGRAM_ALREADY_HAS_ACCOUNT"))
    user = bot.updates()
    await bot.send(user.command(f"/start web_{TOKEN}"))
    assert texts(bot)[-1] == copy("telegram.web_link.has_account", "uz")
    assert state(bot) is None, "never parked in PHONE: their next message must reach the Support Inbox"
    assert ("menu_support" in bot.telegram.last_shown().callback_data()) is (phone is not None)
    assert has_contact_button(bot) is False and created_no_account(bot)


@pytest.mark.parametrize("failure", [refused("TELEGRAM_LINK_STORE_UNAVAILABLE", 503),   # RF4: Redis down
                                     backend_failure("HTTP 404", 404)])                   # a backend without the route
async def test_a_preview_outage_says_try_again_in_a_minute(link_bot, failure):            # D15f
    bot = await link_bot(preview=failure); user = bot.updates()
    await bot.send(user.command(f"/start web_{TOKEN}"))
    assert texts(bot)[-1] == copy("telegram.web_link.failed", "uz")
    assert state(bot) is None and no_language_card(bot) and created_no_account(bot)


@pytest.mark.parametrize("payload", ["web_", "web_x"])
async def test_a_mangled_link_is_a_dead_link(link_bot, payload):                                # RF2
    bot = await link_bot(preview=refused("WEB_LINK_EXPIRED", 400)); user = bot.updates()
    await bot.send(user.command(f"/start {payload}"))
    [call] = posts(bot, PREVIEW)
    assert call.data["token"] == payload[4:]
    assert texts(bot)[-1] == copy("telegram.web_link.expired", "uz")
    assert posts(bot, REGISTER) == [] and no_language_card(bot) and created_no_account(bot)
    assert "referral_code" not in bot.application.user_data[user.user_id]


@pytest.mark.parametrize("code,status,key", [("TELEGRAM_ALREADY_HAS_ACCOUNT", 409, "has_account"),
                                             ("WEB_LINK_EXPIRED", 400, "expired"),
                                             ("WEB_LINK_NOT_AVAILABLE", 409, "expired"),
                                             ("TELEGRAM_LINK_STORE_UNAVAILABLE", 503, "failed")])
async def test_a_refused_confirm_stores_nothing_and_ends(link_bot, code, status, key):
    bot = await link_bot(confirm=refused(code, status)); user = bot.updates()
    await bot.send(user.command(f"/start web_{TOKEN}")); await bot.send(user.tap("weblink_yes", card_id(bot)))
    assert texts(bot)[-1] == copy(f"telegram.web_link.{key}"), "in the page's language (ru), kept from the preview"
    assert bot.tokens.stored == [] and state(bot) is None and created_no_account(bot) and no_language_card(bot)


# ===========================================================================
# Leaving the confirm step any other way (D15b, D15c, D15e)
# ===========================================================================


async def test_the_confirm_step_times_out_with_the_link_copy_never_the_start_copy(link_bot):   # D15b
    bot = await link_bot(); user = bot.updates()
    await bot.send(user.command(f"/start web_{TOKEN}")); bot.telegram.reset()
    result = await fire_conversation_timeout(bot, "registration", user.command(f"/start web_{TOKEN}"))
    assert result == ConversationHandler.END and texts(bot) == [copy("telegram.web_link.expired")]
    assert WEB_LINK_PENDING_KEY not in bot.application.user_data[user.user_id]


@pytest.mark.parametrize("button", ["weblink_yes", "weblink_no"])
async def test_a_confirm_button_from_before_a_restart_says_the_link_expired(link_bot, button):   # D15c
    bot = await link_bot(); user = bot.updates()
    await bot.send(user.tap(button))
    [answer] = bot.telegram.visible_answers
    assert (answer.params["text"], answer.params.get("show_alert")) == (copy("telegram.web_link.expired", "uz"), True)
    assert bot.telegram.of("editMessageReplyMarkup") and posts(bot, CONFIRM) == [] and posts(bot, PREVIEW) == []


async def test_a_plain_start_mid_confirm_forgets_the_link(link_bot):                          # D15e
    bot = await link_bot(); user = bot.updates()
    await bot.send(user.command(f"/start web_{TOKEN}")); card = card_id(bot)
    await bot.send(user.command("/start"))
    assert WEB_LINK_PENDING_KEY not in bot.application.user_data[user.user_id]
    assert state(bot) == SELECT_LANGUAGE
    await bot.send(user.tap("weblink_yes", card))
    assert posts(bot, CONFIRM) == []


OTHER_TOKEN = "Zz9_8Yy7-Xx6Ww5Vv4Uu3Tt2Ss1Rr0Qq"
LABELS = {TOKEN: "A*** · +998***1111", OTHER_TOKEN: "B*** · +998***2222"}


@pytest.mark.parametrize("button", ["weblink_yes", "weblink_no"])
async def test_only_the_newest_confirm_card_answers(link_bot, button):                       # R4, M-F1
    bot = await link_bot(); user = bot.updates()
    bot.backend.route("POST", PREVIEW, lambda call: {"data": {"account_label": LABELS[call.data["token"]],
                                                              "language": "ru"}})
    await bot.send(user.command(f"/start web_{TOKEN}")); card_a = card_id(bot)
    await bot.send(user.command(f"/start web_{OTHER_TOKEN}")); card_b = card_id(bot)
    assert bot.telegram.last_shown().text == copy("telegram.web_link.confirm_prompt", account=LABELS[OTHER_TOKEN])
    bot.telegram.reset()

    await bot.send(user.tap(button, card_a))        # card A names account A, but link B is the one waiting
    [answer] = bot.telegram.visible_answers
    assert (answer.params["text"], answer.params.get("show_alert")) == (copy("telegram.web_link.expired"), True)
    assert [c.params["message_id"] for c in bot.telegram.of("editMessageReplyMarkup")] == [card_a]
    assert posts(bot, CONFIRM) == [] and bot.telegram.shown == [] and state(bot) == WEB_LINK_CONFIRM

    expire_dedup_window()                           # having read the alert, the customer taps card B
    await bot.send(user.tap("weblink_yes", card_b))  # the card that names account B still connects B
    [call] = posts(bot, CONFIRM)
    assert call.data["token"] == OTHER_TOKEN and texts(bot)[-1] == copy("telegram.web_link.connected", name="Zarina")


# ===========================================================================
# After Yes, the number on the account (D12)
# ===========================================================================


async def test_a_number_never_verified_is_confirmed_by_sms_before_the_greeting(link_bot):   # D12
    bot = await link_bot(confirm=confirmed(phone_status="unverified")); user = bot.updates()
    await bot.send(user.command(f"/start web_{TOKEN}")); await bot.send(user.tap("weblink_yes", card_id(bot)))
    [send] = posts(bot, SEND_OTP)
    assert (send.data, send.user_token) == ({"phone": "+998901112233"}, "link-access")
    assert texts(bot)[-1] == copy("telegram.web_link.confirm_number_prompt", phone_masked="+998 ** *** 22 33")
    assert state(bot) == REGISTER_OTP and not has_contact_button(bot)
    await bot.send(user.text("123456"))
    assert len(posts(bot, VERIFY_PHONE)) == 1 and texts(bot)[-1] == copy("telegram.web_link.connected", name="Zarina")
    assert state(bot) is None and created_no_account(bot)


async def test_an_sms_that_cannot_be_sent_still_greets_and_never_offers_the_contact_button(link_bot):   # D12
    bot = await link_bot(confirm=confirmed(phone_status="unverified")); user = bot.updates()
    bot.backend.route("POST", SEND_OTP, lambda _call: backend_failure("SMS gateway down", 502))
    await bot.send(user.command(f"/start web_{TOKEN}")); await bot.send(user.tap("weblink_yes", card_id(bot)))
    assert texts(bot)[-2:] == [copy("telegram.phone.verification_sms_send_failed", error="SMS gateway down"),
                               copy("telegram.web_link.connected", name="Zarina")]
    assert state(bot) is None and not has_contact_button(bot)
    assert WEB_LINK_GREETING_KEY not in bot.application.user_data[user.user_id]


async def test_an_account_without_a_phone_shares_a_contact_and_ends_with_the_greeting(link_bot):   # D12
    bot = await link_bot(confirm=confirmed(phone_status="missing", phone=None)); user = bot.updates()
    await bot.send(user.command(f"/start web_{TOKEN}")); await bot.send(user.tap("weblink_yes", card_id(bot)))
    assert texts(bot)[-1] == copy("telegram.registration.share_contact_prompt")
    assert has_contact_button(bot) and state(bot) == PHONE
    await bot.send(user.contact(MY_PHONE_AS_TELEGRAM_SENDS_IT))
    assert len(posts(bot, CHECK_PHONE)) == 1 and posts(bot, LINK_SEND_OTP) == []
    assert [q for q in bot.database.executed if "phone_verified_at" in q], "a number nobody holds is simply saved"
    assert texts(bot)[-1] == copy("telegram.web_link.connected", name="Zarina")
    assert not set(texts(bot)) & {TRANSLATIONS[(lang, "telegram.registration_complete")] for lang in LANGS}
    assert state(bot) is None and created_no_account(bot)


# What the backend answers when the shared number's account cannot be claimed: it has its own
# Telegram, is inactive or staff, or is one person across phones, which the merge itself would refuse
# (tests/integration/test_customer_telegram_link_api.py pins each refusal).
NOT_CLAIMABLE = {"data": {"available": False, "can_link": False, "existing_user_masked": None}}
# The backend's answer to the code once the usual merge ran: the survivor is the phone holder (R8a).
MERGED = {
    "data": {
        "user": {"first_name": "Co", "last_name": "Worker", "user_type": "individual"},
        "tokens": {"access_token": "merged-access", "refresh_token": "merged-refresh", "expires_in": 3600},
        "linked": True,
    }
}


def share(user, how):
    """The number as the customer gives it: the contact button, or typed."""
    return user.contact(MY_PHONE_AS_TELEGRAM_SENDS_IT) if how == "contact" else user.text(MY_PHONE_E164)


async def offered_a_merge(link_bot, how="contact"):
    """Yes on a link for an account with no phone, then a number another account holds: the merge offer."""
    bot = await link_bot(confirm=confirmed(phone_status="missing", phone=None)); user = bot.updates()
    phone_belongs_to(bot, masked_name="C*** W***")
    await bot.send(user.command(f"/start web_{TOKEN}")); await bot.send(user.tap("weblink_yes", card_id(bot)))
    await bot.send(share(user, how))
    return bot, user


@pytest.mark.parametrize("how", ["contact", "typed"])
async def test_a_number_held_by_another_account_gets_the_usual_merge_and_its_own_success(link_bot, how):  # R8
    bot, user = await offered_a_merge(link_bot, how)
    bot.backend.route("POST", LINK_VERIFY, lambda _call: MERGED)
    prompt = bot.telegram.last_shown()
    assert (prompt.text, prompt.callback_data()) == (
        "This number already belongs to C*** W***. Link the accounts?", ["link_yes", "link_no"]
    )
    assert state(bot) == LINK_ACCOUNT_CONFIRM

    await bot.send(user.tap("link_yes"))
    assert payloads(bot, LINK_SEND_OTP) == [{"telegram_id": DEFAULT_USER_ID, "phone": MY_PHONE_E164}]
    assert state(bot) == LINK_ACCOUNT_OTP
    await bot.send(user.text("123456"))

    assert payloads(bot, LINK_VERIFY) == [{"telegram_id": DEFAULT_USER_ID, "otp": "123456"}]
    shown = bot.telegram.last_shown()
    assert shown.text == "Welcome back, Co! Accounts linked" and "menu_support" in shown.callback_data()
    assert bot.tokens.stored[-1] == {"user_id": DEFAULT_USER_ID, "access_token": "merged-access",
                                     "refresh_token": "merged-refresh", "expires_in": 3600}
    assert not {copy("telegram.web_link.connected", lang, name="Zarina") for lang in LANGS} & set(texts(bot))
    assert state(bot) is None and WEB_LINK_GREETING_KEY not in bot.application.user_data[user.user_id]
    assert created_no_account(bot)


def verify_refused(code, key, language, status):
    """The link verify's refusal as `@handle_exceptions` sends it, its message in the customer's language."""
    message = BACKEND_TRANSLATIONS[key][language]
    return backend_failure(message, status, data={"error_code": code, "message": message})


async def test_the_expired_code_exit_ends_without_the_deferred_greeting(link_bot):   # R8
    bot, user = await offered_a_merge(link_bot)
    # The holder is gone (api.auth.web_account_not_found): the code can no longer link anything.
    bot.backend.route("POST", LINK_VERIFY, lambda _call: verify_refused(
        "USER_NOT_FOUND", "api.auth.web_account_not_found", "en", 404))
    await bot.send(user.tap("link_yes")); await bot.send(user.text("123456"))
    assert texts(bot)[-1] == "That code expired. Start again"
    assert state(bot) is None and WEB_LINK_GREETING_KEY not in bot.application.user_data[user.user_id]


@pytest.mark.parametrize("language", ["uz", "ru"])
@pytest.mark.parametrize("code,key,status", [
    ("OTP_EXPIRED", "api.auth.pending_link_not_found", 404),  # the 10-minute link request is gone
    ("OTP_EXPIRED", "api.auth.link_otp_invalid", 400),        # the code's own 3 minutes are up
])
async def test_an_expired_code_ends_the_step_in_every_language(link_bot, language, code, key, status):   # T6-M8
    bot, user = await offered_a_merge(link_bot)
    bot.backend.route("POST", LINK_VERIFY, lambda _call: verify_refused(code, key, language, status))
    await bot.send(user.tap("link_yes")); await bot.send(user.text("123456"))
    assert texts(bot)[-1] == "That code expired. Start again"
    assert state(bot) is None and WEB_LINK_GREETING_KEY not in bot.application.user_data[user.user_id]


@pytest.mark.parametrize("language", ["en", "uz", "ru"])
async def test_a_wrong_code_asks_for_it_again(link_bot, language):   # T6-M8
    bot, user = await offered_a_merge(link_bot)
    bot.backend.route("POST", LINK_VERIFY, lambda _call: verify_refused(
        "INVALID_OTP", "api.auth.link_otp_invalid", language, 400))
    await bot.send(user.tap("link_yes")); await bot.send(user.text("123456"))
    assert state(bot) == LINK_ACCOUNT_OTP
    assert BACKEND_TRANSLATIONS["api.auth.link_otp_invalid"][language] in texts(bot)[-1]
    assert bot.application.user_data[user.user_id][WEB_LINK_GREETING_KEY]  # the step goes on; so does the greeting


async def test_a_step_that_fails_unexpectedly_leaves_no_deferred_greeting(link_bot):   # T6-M6
    bot, user = await offered_a_merge(link_bot)
    await bot.send(user.tap("link_yes"))
    bot.backend.route("POST", LINK_VERIFY, lambda _call: verify_refused(
        "INVALID_OTP", "api.auth.link_otp_invalid", "en", 400))
    bot.telegram.fail("sendMessage", "Forbidden: bot was blocked by the user", 403)  # the re-prompt cannot go out
    await bot.send(user.text("123456"))
    assert state(bot) is None and WEB_LINK_GREETING_KEY not in bot.application.user_data[user.user_id]


@pytest.mark.parametrize("how", ["contact", "typed"])
async def test_a_number_whose_account_cannot_be_claimed_points_to_the_chat_and_greets(link_bot, how):   # D12, R6
    bot = await link_bot(confirm=confirmed(phone_status="missing", phone=None)); user = bot.updates()
    bot.backend.route("POST", CHECK_PHONE, lambda _call: NOT_CLAIMABLE)
    await bot.send(user.command(f"/start web_{TOKEN}")); await bot.send(user.tap("weblink_yes", card_id(bot)))
    await bot.send(share(user, how))
    assert len(posts(bot, CHECK_PHONE)) == 1 and posts(bot, LINK_SEND_OTP) == []
    assert texts(bot)[-2:] == [copy("telegram.web_link.has_account"), copy("telegram.web_link.connected", name="Zarina")]
    assert bot.telegram.shown[-2].reply_markup == {"remove_keyboard": True}, "the contact keyboard goes away"
    assert not [shown for shown in bot.telegram.shown if "link_yes" in shown.callback_data()]
    assert state(bot) is None and created_no_account(bot)
    assert WEB_LINK_GREETING_KEY not in bot.application.user_data[user.user_id]


async def test_in_plain_signup_a_number_held_elsewhere_still_asks_for_another(link_bot):   # _number_taken's other arm
    bot = await link_bot(); user = bot.updates()
    phone_belongs_to(bot, can_link=False)
    await reach_phone_step(bot, user)
    await bot.send(user.contact(MY_PHONE_AS_TELEGRAM_SENDS_IT))
    assert texts(bot)[-1] == TRANSLATIONS["telegram.phone.already_linked_other_account"]
    assert has_contact_button(bot) and state(bot) == PHONE
