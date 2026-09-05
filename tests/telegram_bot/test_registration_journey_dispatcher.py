"""The customer-bot onboarding journey, driven through the REAL dispatcher.

WHY THIS FILE EXISTS
--------------------
Registration is the only flow every single customer walks, exactly once, with
no prior familiarity with the bot — and it is the flow with the most wiring per
step: five conversation states, two entry paths for the phone (the CONTACT
button and typed text), two OTP endpoints that must never be confused, and a
branch that merges a Telegram account into an existing customer record.

`tests/telegram_bot/test_registration_phone_flow.py` calls those handlers
directly with hand-rolled dummies. That proves each handler does the right
thing *when it is called*. It cannot prove any of them is reached, that the
button a customer taps is registered in the state they are parked in, or that a
second handler group is quietly re-processing the same update. Those are the
seams where this project's shipped defects live.

So every update here goes in through `Application.process_update`. The
conversation state machine, the handler groups, the real keyboards and the real
`api_client` endpoint paths are all in the loop; only Telegram, the backend and
the bot's own SQL are faked.
"""

import pytest
from telegram import Update
from telegram.ext import (
    CallbackContext,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
)

import utils as utils_module
from handlers.profile import (
    LINK_ACCOUNT_CONFIRM,
    LINK_ACCOUNT_OTP,
    PHONE,
    REGISTER_OTP,
    SELECT_LANGUAGE,
)

from tests.telegram_bot.ptb_harness import (
    DEFAULT_USER_ID,
    FakeDatabase,
    FakeTokenManager,
    backend_failure,
    build_bot_harness,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


# --- the endpoints this journey is allowed to touch -------------------------
REGISTER = "/api/v1/auth/telegram-register"
CHECK_PHONE = "/api/v1/auth/check-phone-availability"
SEND_OTP = "/api/v1/auth/send-otp"
VERIFY_PHONE = "/api/v1/auth/verify-phone"
LINK_SEND_OTP = "/api/v1/auth/link-phone-account/send-otp"
LINK_VERIFY = "/api/v1/auth/link-phone-account/verify"
SUPPORT_MESSAGES = "/api/v1/support/messages"

# A real Beeline (90) and a real Ucell (93) Uzbek mobile. Both must survive
# `shared.validators` — the SSOT the bot delegates to — or the flow cannot run.
MY_PHONE_AS_TELEGRAM_SENDS_IT = "998901112233"   # Telegram often omits the '+'
MY_PHONE_E164 = "+998901112233"
OTHER_PHONE_E164 = "+998935554433"

# `maybe_remove_stale_reply_keyboard` posts this word-joiner and deletes it
# again; it is never something a customer reads.
KEYBOARD_CLEANUP_FILLER = "⁠"


# ---------------------------------------------------------------------------
# Translations — distinct per language so a mis-routed language is VISIBLE
# ---------------------------------------------------------------------------

def _trilingual(key, en, uz, ru):
    return {("en", key): en, ("uz", key): uz, ("ru", key): ru}


TRANSLATIONS = {
    # Bare keys: language-insensitive for the purposes of these tests.
    "telegram.profile.share_phone": "SHARE-CONTACT-BUTTON",
    "telegram.phone.phone_accepted": "Phone accepted",
    "telegram.phone.invalid_format": "That is not a valid Uzbek mobile number",
    "telegram.registration.share_own_contact": "Please share YOUR OWN contact",
    "telegram.registration.share_contact_prompt": "Share your contact to finish signing up",
    "telegram.phone.already_registered_link_prompt": (
        "This number already belongs to {masked_name}. Link the accounts?"
    ),
    "telegram.phone.link_yes_button": "Yes, link them",
    "telegram.phone.link_no_button": "No, use another number",
    "telegram.phone.already_linked_other_account": (
        "That number is already tied to a different Telegram account"
    ),
    "telegram.phone.verification_code_sent_to_phone_prompt": "Code sent to {phone_masked}",
    "telegram.phone.verification_sms_send_failed": "Could not send the SMS: {error}",
    "telegram.phone.verify_unavailable": "Verification is unavailable, try again",
    "telegram.phone.verify_unavailable_now": "Verification is not available right now",
    "telegram.phone.enter_valid_6_digit_code": "Enter the 6-digit code",
    "telegram.bot.otp.invalid_format": "The code must be 6 digits",
    "telegram.phone.verification_failed_with_error_retry": "Wrong code ({error}). Try again",
    "telegram.phone.verification_code_expired_start_again": "That code expired. Start again",
    "telegram.phone.session_expired_start_again": "Your session expired. Start again",
    "telegram.phone.accounts_linked_success": "Welcome back, {name}! Accounts linked",
    "telegram.phone.share_different_phone_prompt": "Alright, use a different number",
    "telegram.phone.share_phone_using_button": "Tap the button below to share your phone",
    "telegram.phone.too_many_verification_attempts": "Too many code requests, wait a bit",
    "telegram.registration.failed_toast": "Registration failed",
    "telegram.registration.failed_contact_support": "We could not sign you up. Contact support",
    "telegram.registration.language_updated_toast": "Language updated",
    "telegram.action_cancelled_short": "Cancelled",
    "telegram.action_cancelled": "Cancelled. Here is the menu",
    "telegram.cancel": "Cancel",
    # Produced ONLY by the standalone language-change handler in group 0, so
    # seeing it during signup means that handler ran when it must not have —
    # see test_the_language_tap_is_processed_by_the_conversation_alone.
    "telegram.language.already_selected": "you already use this language",
}
TRANSLATIONS.update(_trilingual(
    "telegram.registration_welcome",
    "en|Welcome to BlueStream", "uz|BlueStream'ga xush kelibsiz", "ru|Добро пожаловать",
))
TRANSLATIONS.update(_trilingual(
    "telegram.registration.enter_phone",
    "en|Send us your phone number", "uz|Telefon raqamingizni yuboring", "ru|Отправьте номер телефона",
))
TRANSLATIONS.update(_trilingual(
    "telegram.registration_complete",
    "en|You are all set", "uz|Ro'yxatdan o'tdingiz", "ru|Регистрация завершена",
))
TRANSLATIONS.update(_trilingual(
    "telegram.welcome", "en|Welcome back", "uz|Yana xush kelibsiz", "ru|С возвращением",
))
TRANSLATIONS.update(_trilingual(
    "telegram.registration.flow_timed_out",
    "en|Sign-up timed out. Send /start to begin again",
    "uz|Ro'yxatdan o'tish vaqti tugadi. Qaytadan boshlash uchun /start yuboring",
    "ru|Время регистрации истекло. Отправьте /start, чтобы начать заново",
))


# ---------------------------------------------------------------------------
# Seams this file needs on top of the shared harness
# ---------------------------------------------------------------------------


class RegistrationDatabase(FakeDatabase):
    """The bot's own `users` table — starting EMPTY, like a real new customer.

    The shared `FakeDatabase` always answers with a fully-registered customer,
    which is precisely the state registration is supposed to CREATE. Every
    branch of `start_registration_new` turns on whether this row exists and
    whether it carries a phone, so the row has to be able to not exist, and to
    gain a phone the way production gains one: written by the handler.
    """

    def __init__(self, row=None):
        super().__init__()
        self.row = row

    async def execute(self, query, *args):
        self.executed.append(query)
        if self.row is not None:
            if "phone_verified_at" in query and args:
                self.row["phone"] = args[0]
            elif "SET preferred_language" in query and args:
                self.row["preferred_language"] = args[0]
        return "UPDATE 1"

    async def fetchone(self, query, *args):
        if "FROM users" in query:
            return dict(self.row) if self.row else None
        return None

    async def fetchval(self, query, *args):
        if "preferred_language" in query:
            return (self.row or {}).get("preferred_language")
        if "loyalty" in query.lower():
            return True
        return None


class RecordingTokenManager(FakeTokenManager):
    """Remembers WHAT was cached, not merely that caching happened.

    Registration and account-merge both hand the bot a fresh token pair. If the
    bot keeps a stale token instead, every later call the customer makes is
    authenticated as somebody else (or as nobody), which is invisible until it
    is a support ticket. So the tests assert the exact pair.
    """

    def __init__(self):
        super().__init__()
        self.stored = []

    async def store_tokens(self, user_id, access_token=None, refresh_token=None,
                           expires_in=None, *_a, **_kw):
        self.stored.append(
            {
                "user_id": user_id,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_in": expires_in,
            }
        )
        self.tokens[user_id] = access_token or "test-access-token"
        return True


def _always(value):
    async def _answer(*_args, **_kwargs):
        return value

    return _answer


@pytest.fixture
async def make_bot(monkeypatch):
    """Build a fresh customer bot with an EMPTY users table.

    Both rate limiters are answered from memory rather than from the test
    Redis: production runs with a healthy Redis, so "allowed" is the
    production-normal answer, and leaving them on a shared Redis DB would make
    one test's traffic decide another test's outcome.
    """

    async def _make(*, row=None, allow_otp_requests=True):
        db = RegistrationDatabase(row)
        harness = await build_bot_harness(
            monkeypatch, translations=TRANSLATIONS, database=db
        )

        monkeypatch.setattr(utils_module.rate_limiter, "allow_request", _always(True))
        monkeypatch.setattr(
            utils_module.otp_rate_limiter, "allow_otp_request", _always(allow_otp_requests)
        )

        tokens = RecordingTokenManager()
        harness.application.bot_data["token_manager"] = tokens
        harness.tokens = tokens

        # The backend is what creates the users row; the bot then reads it.
        # Wiring the fake the same way keeps "has the customer been created
        # yet?" a single fact instead of two that can drift apart.
        def _register(call):
            db.row = {
                "id": 4242,
                "telegram_id": str(DEFAULT_USER_ID),
                "first_name": "Kamola",
                "phone": None,
                "preferred_language": (call.data or {}).get("language_code"),
                "role": "customer",
                "status": "active",
                "bot_state": "{}",
                "user_type": "individual",
            }
            return {
                "data": {
                    "user": {"id": 4242, "telegram_id": DEFAULT_USER_ID},
                    "tokens": {
                        "access_token": "fresh-registration-access",
                        "refresh_token": "fresh-registration-refresh",
                        "expires_in": 7200,
                    },
                }
            }

        harness.backend.route("POST", REGISTER, _register)
        harness.backend.route("POST", CHECK_PHONE, lambda _c: {"data": {"available": True}})
        harness.backend.route(
            "POST", SEND_OTP, lambda _c: {"data": {"phone_masked": "+998 ** *** 22 33"}}
        )
        return harness

    return _make


@pytest.fixture
async def bot(make_bot):
    return await make_bot()


@pytest.fixture
def user(bot):
    return bot.updates()


# ---------------------------------------------------------------------------
# Reading the conversation the way the customer reads it
# ---------------------------------------------------------------------------


def texts(bot):
    """Everything the customer actually reads, in order."""
    return [
        call.text
        for call in bot.telegram.shown
        if call.text and call.text != KEYBOARD_CLEANUP_FILLER
    ]


def last_text(bot):
    shown = texts(bot)
    assert shown, "the bot showed the customer nothing at all"
    return shown[-1]


def toasts(bot):
    return [call.params.get("text", "") for call in bot.telegram.of("answerCallbackQuery")]


def posts(bot, endpoint):
    return [c for c in bot.backend.calls if c.method == "POST" and c.endpoint == endpoint]


def payloads(bot, endpoint):
    return [c.data for c in posts(bot, endpoint)]


def state(bot):
    return bot.conversation_state("registration")


def keyboard_of(call):
    markup = call.reply_markup
    return markup.get("inline_keyboard") or markup.get("keyboard") or []


def has_contact_button(bot):
    """True when the LAST thing shown carries the request_contact keyboard."""
    for call in reversed(bot.telegram.shown):
        if call.text == KEYBOARD_CLEANUP_FILLER:
            continue
        for row in keyboard_of(call):
            for button in row:
                if isinstance(button, dict) and button.get("request_contact"):
                    return True
        return False
    return False


# --- building the two updates UpdateFactory cannot build --------------------


def start_with_payload(user, payload):
    """`/start ref_CODE`, entity-tagged the way Telegram tags a deep link.

    The bot_command entity must span only "/start"; PTB slices the command out
    of the text by that length, so an entity covering the payload too makes the
    CommandHandler silently not match and the test would prove nothing.
    """
    return Update.de_json(
        {
            "update_id": user._next_update_id(),
            "message": user._message_envelope(
                text=f"/start {payload}",
                entities=[{"type": "bot_command", "offset": 0, "length": len("/start")}],
            ),
        },
        user.bot,
    )


def forwarded_contact(user, phone_number, owner_user_id):
    """Somebody ELSE's contact card, forwarded into the chat."""
    return Update.de_json(
        {
            "update_id": user._next_update_id(),
            "message": user._message_envelope(
                contact={
                    "phone_number": phone_number,
                    "first_name": "Dilnoza",
                    "user_id": owner_user_id,
                }
            ),
        },
        user.bot,
    )


# --- backend scripts --------------------------------------------------------


def phone_belongs_to(bot, *, masked_name="K***a", can_link=True):
    bot.backend.route(
        "POST",
        CHECK_PHONE,
        lambda _c: {
            "data": {
                "available": False,
                "can_link": can_link,
                "existing_user_masked": {"name": masked_name},
            }
        },
    )


# --- walking the flow -------------------------------------------------------


async def reach_language_step(bot, user):
    await bot.send(user.command("/start"))
    return state(bot)


async def reach_phone_step(bot, user, language="uz"):
    await reach_language_step(bot, user)
    await bot.send(user.tap(f"set_language_{language}"))
    return state(bot)


async def reach_register_otp_step(bot, user, language="uz"):
    await reach_phone_step(bot, user, language)
    await bot.send(user.text(MY_PHONE_E164))
    return state(bot)


async def reach_link_confirm_step(bot, user, language="uz"):
    await reach_phone_step(bot, user, language)
    phone_belongs_to(bot)
    await bot.send(user.contact(MY_PHONE_AS_TELEGRAM_SENDS_IT))
    return state(bot)


async def reach_link_otp_step(bot, user, language="uz"):
    await reach_link_confirm_step(bot, user, language)
    await bot.send(user.tap("link_yes"))
    return state(bot)


async def fire_conversation_timeout(bot, name, carrier):
    """Run the TIMEOUT step the way PTB runs it when the timer fires.

    A timeout never arrives through `process_update`: `ConversationHandler`
    keeps the LAST real update, and when the job fires it walks the handlers
    registered under `ConversationHandler.TIMEOUT` itself, awaiting the first
    one that accepts that update. Mirrored here so the test drives the real
    registration table instead of sleeping out a 300-second timer.

    Raises rather than skipping when nothing accepts the update — "the flow
    expired in silence" is the defect, not a reason to pass.
    """
    handler = bot.conversation(name)
    context = CallbackContext.from_update(carrier, bot.application)
    for inner in handler.states.get(ConversationHandler.TIMEOUT) or []:
        check = inner.check_update(carrier)
        if check is None or check is False:
            continue
        return await inner.handle_update(carrier, bot.application, check, context)
    raise AssertionError(
        f"no TIMEOUT handler in {name!r} accepts {carrier}; this customer's "
        "flow would end without a word"
    )


# ===========================================================================
# 1. The whole happy path, both phone entry methods
# ===========================================================================


async def test_a_brand_new_customer_who_taps_share_contact_finishes_registration_in_one_pass(
    bot, user
):
    """The single journey every customer makes exactly once.

    If this breaks nobody can become a customer at all, and the failure is
    invisible from the backend's side — the bot simply stops sending anything
    after a step, and the person deletes the chat.
    """
    assert await reach_language_step(bot, user) == SELECT_LANGUAGE

    welcome = texts(bot)[0]
    for expected in ("en|Welcome to BlueStream", "uz|BlueStream'ga xush kelibsiz",
                     "ru|Добро пожаловать"):
        assert expected in welcome, (
            "the first screen is shown BEFORE the customer picks a language, so it "
            "must be readable in all three — showing one language guesses wrong for "
            f"two thirds of new customers: {welcome!r}"
        )
    assert bot.telegram.shown[0].callback_data() == [
        "set_language_en", "set_language_uz", "set_language_ru",
    ]

    await bot.send(user.tap("set_language_uz"))

    assert payloads(bot, REGISTER) == [
        {
            "telegram_id": DEFAULT_USER_ID,
            "first_name": "Kamola",
            "last_name": None,
            "username": "kamola_test",
            "language_code": "uz",
        }
    ]
    assert bot.tokens.stored == [
        {
            "user_id": DEFAULT_USER_ID,
            "access_token": "fresh-registration-access",
            "refresh_token": "fresh-registration-refresh",
            "expires_in": 7200,
        }
    ], "the token minted by registration must replace whatever was cached before it"
    assert state(bot) == PHONE
    assert has_contact_button(bot), (
        "without the request_contact keyboard the customer has to type their "
        "number, which is the slower, error-prone, OTP-costing path"
    )

    bot.telegram.reset()
    await bot.send(user.contact(MY_PHONE_AS_TELEGRAM_SENDS_IT))

    assert payloads(bot, CHECK_PHONE) == [
        {"telegram_id": DEFAULT_USER_ID, "phone": MY_PHONE_E164}
    ], "Telegram hands over '998...' with no '+'; the backend is only ever told E.164"
    assert posts(bot, SEND_OTP) == [], (
        "a contact shared through Telegram's own button is already proof of "
        "ownership — charging an SMS for it is money burned on every signup"
    )
    assert bot.database.row["phone"] == MY_PHONE_E164
    assert state(bot) is None, "registration must END, not leave the customer parked"
    assert "uz|Ro'yxatdan o'tdingiz" in texts(bot)


async def test_the_language_tapped_at_the_first_step_is_the_language_every_later_step_uses(
    bot, user
):
    """A customer who picks Russian must not be walked through the rest of
    signup in Uzbek. The language is chosen before the users row exists, so
    every later step has to read it back from what registration stored — the
    exact place where the bot used to fall back to the Telegram client locale.
    """
    await reach_language_step(bot, user)
    bot.telegram.reset()

    await bot.send(user.tap("set_language_ru"))

    assert payloads(bot, REGISTER)[0]["language_code"] == "ru"
    assert "ru|Отправьте номер телефона" in texts(bot)

    bot.telegram.reset()
    await bot.send(user.contact(MY_PHONE_AS_TELEGRAM_SENDS_IT))

    assert "ru|Регистрация завершена" in texts(bot)
    assert not any("uz|" in text or "en|" in text for text in texts(bot)), (
        f"the flow switched language mid-signup: {texts(bot)}"
    )


async def test_a_typed_phone_number_walks_through_otp_and_survives_a_wrong_code(bot, user):
    """The typed-phone path, including the two things people really do: fat-finger
    the code, then read it properly off the SMS.

    A wrong code must cost the customer one retry, not the whole registration —
    and a retry must go back to the REGISTRATION verify endpoint every time.
    """
    await reach_phone_step(bot, user)
    bot.telegram.reset()

    await bot.send(user.text(MY_PHONE_E164))

    assert payloads(bot, CHECK_PHONE) == [
        {"telegram_id": DEFAULT_USER_ID, "phone": MY_PHONE_E164}
    ]
    assert payloads(bot, SEND_OTP) == [{"phone": MY_PHONE_E164}]
    assert state(bot) == REGISTER_OTP
    assert "Code sent to +998 ** *** 22 33" in texts(bot), (
        "the prompt must echo the backend's MASKED number; printing the full "
        "number back into the chat leaks it to anyone the screen is shown to"
    )

    # Fat-fingered: five digits.
    bot.telegram.reset()
    await bot.send(user.text("12345"))
    assert last_text(bot) == "The code must be 6 digits"
    assert state(bot) == REGISTER_OTP
    assert posts(bot, VERIFY_PHONE) == [], "a malformed code must not cost a backend call"

    # Right shape, wrong code.
    bot.backend.route(
        "POST",
        VERIFY_PHONE,
        lambda _c: backend_failure("Invalid verification code", status_code=400),
    )
    bot.telegram.reset()
    await bot.send(user.text("999999"))
    assert payloads(bot, VERIFY_PHONE) == [{"otp": "999999"}]
    assert last_text(bot) == "Wrong code (Invalid verification code). Try again"
    assert state(bot) == REGISTER_OTP, "one wrong digit must not end registration"

    # Read off the SMS properly.
    bot.backend.route("POST", VERIFY_PHONE, lambda _c: {"data": {"verified": True}})
    bot.telegram.reset()
    await bot.send(user.text("123456"))

    assert payloads(bot, VERIFY_PHONE) == [{"otp": "999999"}, {"otp": "123456"}]
    assert "uz|Ro'yxatdan o'tdingiz" in texts(bot)
    assert state(bot) is None


# ===========================================================================
# 2. Phones the bot must refuse
# ===========================================================================


@pytest.mark.parametrize(
    "typed,why",
    [
        ("12345", "far too short"),
        ("+7 916 123 45 67", "a valid RUSSIAN mobile — valid, but not ours"),
        ("+998 71 200 00 00", "a Tashkent LANDLINE, which cannot receive an SMS OTP"),
        ("+99890111", "a half-typed number"),
    ],
)
async def test_a_phone_the_validator_rejects_never_reaches_the_backend(bot, user, typed, why):
    """`shared.validators` is the SSOT both bot and backend use. If a rejected
    number still reached `check-phone-availability`, the backend would be the
    only thing standing between a junk number and an SMS bill — and the
    customer would be parked waiting for a code that can never arrive.
    """
    await reach_phone_step(bot, user)
    bot.telegram.reset()

    await bot.send(user.text(typed))

    assert posts(bot, CHECK_PHONE) == [], f"{typed!r} ({why}) was sent to the backend"
    assert posts(bot, SEND_OTP) == [], f"{typed!r} ({why}) cost an SMS"
    assert last_text(bot) == "That is not a valid Uzbek mobile number"
    assert state(bot) == PHONE, "a rejected number must leave the customer able to retry"


async def test_a_number_typed_with_words_around_it_still_reaches_the_backend_as_e164(
    bot, user
):
    """People answer "send your phone number" in a sentence — "mening raqamim
    901112233". `shared.validators` deliberately pulls the nine national digits
    out of whatever it is given, so this is a customer who gets registered
    rather than one who gets told off. What must never vary is the shape the
    backend is told: E.164, every time, whatever the customer typed.
    """
    await reach_phone_step(bot, user)
    bot.telegram.reset()

    await bot.send(user.text("mening raqamim 90 111 22 33"))

    assert payloads(bot, CHECK_PHONE) == [
        {"telegram_id": DEFAULT_USER_ID, "phone": MY_PHONE_E164}
    ]
    assert payloads(bot, SEND_OTP) == [{"phone": MY_PHONE_E164}]
    assert state(bot) == REGISTER_OTP


async def test_forwarding_someone_elses_contact_card_is_refused(bot, user):
    """People forward a friend's contact card by accident — and, occasionally,
    on purpose. Accepting it would register this Telegram account against
    somebody else's phone number, and every delivery SMS after that goes to a
    stranger.
    """
    await reach_phone_step(bot, user)
    bot.telegram.reset()

    await bot.send(forwarded_contact(user, OTHER_PHONE_E164, owner_user_id=DEFAULT_USER_ID + 1))

    assert posts(bot, CHECK_PHONE) == []
    assert bot.database.row["phone"] is None
    assert last_text(bot) == "Please share YOUR OWN contact"
    assert state(bot) == PHONE

    # ...and the customer can still finish with their own.
    await bot.send(user.contact(MY_PHONE_AS_TELEGRAM_SENDS_IT))
    assert bot.database.row["phone"] == MY_PHONE_E164
    assert state(bot) is None


# ===========================================================================
# 3. The account-LINK branch
# ===========================================================================


async def test_a_phone_that_already_has_an_account_offers_a_link_and_the_code_merges_it(
    bot, user
):
    """A customer who ordered by phone before, or switched Telegram accounts,
    reaches signup with a number the backend already knows. Losing this branch
    means losing their order history, their bottle balance and their AquaCoins
    — silently, because a brand-new empty account looks like a working one.
    """
    assert await reach_link_confirm_step(bot, user) == LINK_ACCOUNT_CONFIRM

    prompt = bot.telegram.shown[-1]
    assert prompt.text == "This number already belongs to K***a. Link the accounts?"
    assert MY_PHONE_E164 not in prompt.text, (
        "the prompt is about an account the customer may not own — the backend "
        "sends a MASKED name for exactly that reason"
    )
    assert prompt.callback_data() == ["link_yes", "link_no"]

    bot.telegram.reset()
    await bot.send(user.tap("link_yes"))

    assert payloads(bot, LINK_SEND_OTP) == [
        {"telegram_id": DEFAULT_USER_ID, "phone": MY_PHONE_E164}
    ]
    assert state(bot) == LINK_ACCOUNT_OTP

    bot.backend.route(
        "POST",
        LINK_VERIFY,
        lambda _c: {
            "data": {
                "user": {"first_name": "Kamola"},
                "tokens": {
                    "access_token": "merged-access",
                    "refresh_token": "merged-refresh",
                    "expires_in": 3600,
                },
            }
        },
    )
    bot.telegram.reset()
    await bot.send(user.text("123456"))

    assert payloads(bot, LINK_VERIFY) == [{"telegram_id": DEFAULT_USER_ID, "otp": "123456"}]
    assert last_text(bot) == "Welcome back, Kamola! Accounts linked"
    assert state(bot) is None
    assert bot.tokens.stored[-1] == {
        "user_id": DEFAULT_USER_ID,
        "access_token": "merged-access",
        "refresh_token": "merged-refresh",
        "expires_in": 3600,
    }, (
        "after a merge the customer IS the older account; keeping the "
        "pre-merge token means every later call reads the empty account"
    )


async def test_a_link_code_is_never_verified_against_the_registration_endpoint(bot, user):
    """Two OTP endpoints, six-digit codes, adjacent code paths. Verifying a
    LINK code with `verify-phone` would mark this fresh empty account's phone
    as verified without merging anything, leaving two accounts on one number —
    which is the state the whole link branch exists to prevent.
    """
    await reach_link_otp_step(bot, user)

    # Not digits at all: rejected before any endpoint is chosen.
    bot.telegram.reset()
    await bot.send(user.text("abc123"))
    assert last_text(bot) == "Enter the 6-digit code"
    assert state(bot) == LINK_ACCOUNT_OTP
    assert bot.backend.calls[-1].endpoint != LINK_VERIFY

    bot.backend.route(
        "POST", LINK_VERIFY, lambda _c: backend_failure("Invalid code", status_code=400)
    )
    await bot.send(user.text("000000"))
    assert state(bot) == LINK_ACCOUNT_OTP

    bot.backend.route("POST", LINK_VERIFY, lambda _c: {"data": {"user": {"first_name": "K"}}})
    await bot.send(user.text("123456"))

    assert payloads(bot, LINK_VERIFY) == [
        {"telegram_id": DEFAULT_USER_ID, "otp": "000000"},
        {"telegram_id": DEFAULT_USER_ID, "otp": "123456"},
    ]
    assert posts(bot, VERIFY_PHONE) == [], (
        "the merge path must never touch the registration verify endpoint"
    )


async def test_saying_no_to_the_link_leads_back_to_a_phone_step_that_still_works(bot, user):
    """"No, use another number" is the branch a person takes when the masked
    name is not theirs. It has to be a real way forward, not a dead end: the
    contact keyboard must come back, the abandoned number must be forgotten,
    and a second number must be checked afresh.
    """
    await reach_link_confirm_step(bot, user)
    bot.telegram.reset()

    await bot.send(user.tap("link_no"))

    assert state(bot) == PHONE
    assert "Alright, use a different number" in texts(bot)
    assert has_contact_button(bot), (
        "declining the link removes the previous keyboard; without a new one "
        "the customer is looking at a chat with no button and no instruction"
    )

    bot.backend.route("POST", CHECK_PHONE, lambda _c: {"data": {"available": True}})
    await bot.send(user.contact(OTHER_PHONE_E164))

    assert payloads(bot, CHECK_PHONE)[-1] == {
        "telegram_id": DEFAULT_USER_ID, "phone": OTHER_PHONE_E164
    }
    assert posts(bot, LINK_SEND_OTP) == [], "the declined number must not still get an OTP"
    assert bot.database.row["phone"] == OTHER_PHONE_E164
    assert state(bot) is None


async def test_a_number_owned_by_another_telegram_account_is_refused_without_an_otp(bot, user):
    """`can_link=False` means the number is already bound to a DIFFERENT Telegram
    account. Offering a link there would let anyone who knows a phone number
    take over that customer's account with one SMS.
    """
    await reach_phone_step(bot, user)
    phone_belongs_to(bot, can_link=False)
    bot.telegram.reset()

    await bot.send(user.contact(MY_PHONE_AS_TELEGRAM_SENDS_IT))

    assert posts(bot, LINK_SEND_OTP) == []
    assert last_text(bot) == "That number is already tied to a different Telegram account"
    assert has_contact_button(bot)
    assert state(bot) == PHONE
    assert bot.database.row["phone"] is None


async def test_a_rate_limited_link_request_keeps_the_customer_inside_the_flow(make_bot):
    """Three OTP requests in five minutes is the cap. Hitting it must slow the
    customer down, not eject them: the conversation has to stay alive so the
    next attempt is still part of signup rather than a stray message.
    """
    bot = await make_bot(allow_otp_requests=False)
    user = bot.updates()
    await reach_link_confirm_step(bot, user)
    bot.telegram.reset()

    await bot.send(user.tap("link_yes"))

    assert posts(bot, LINK_SEND_OTP) == [], "a rate-limited request must not reach the SMS gateway"
    assert last_text(bot) == "Too many code requests, wait a bit"
    assert state(bot) == PHONE


async def test_tapping_share_contact_twice_does_not_check_the_phone_twice(bot, user):
    """Reply-keyboard taps are NOT covered by the callback-dedup middleware —
    that only guards inline buttons. A person on a slow connection who sees no
    reply taps Share again, so the second contact has to be harmless: one
    availability check, one phone write, no second welcome.
    """
    await reach_phone_step(bot, user)
    await bot.send(user.contact(MY_PHONE_AS_TELEGRAM_SENDS_IT))
    assert state(bot) is None
    after_first = len(texts(bot))

    await bot.send(user.contact(MY_PHONE_AS_TELEGRAM_SENDS_IT))

    assert len(posts(bot, CHECK_PHONE)) == 1, (
        f"the phone was checked twice: {payloads(bot, CHECK_PHONE)}"
    )
    assert len(texts(bot)) == after_first, (
        f"the duplicate tap produced a second reply: {texts(bot)[after_first:]}"
    )


async def test_tapping_start_while_waiting_for_the_sms_still_leaves_a_way_to_finish(
    bot, user
):
    """People tap the pinned Start button when a code is slow to arrive. That
    re-enters the conversation at the PHONE step, which means the code they are
    about to paste is no longer expected — so the bot must accept the number
    again and send a FRESH code, not leave them alternating between "not a
    valid number" and a code nothing is listening for.
    """
    await reach_register_otp_step(bot, user)
    assert state(bot) == REGISTER_OTP

    await bot.send(user.command("/start"))
    assert state(bot) == PHONE

    # The code finally arrives — and is now read as a phone number.
    bot.telegram.reset()
    await bot.send(user.text("123456"))
    assert posts(bot, VERIFY_PHONE) == [], "a stale code must not be verified out of state"
    assert last_text(bot) == "That is not a valid Uzbek mobile number"

    # The way out: give the number again and get a new code.
    await bot.send(user.text(MY_PHONE_E164))
    assert len(posts(bot, SEND_OTP)) == 2, "no fresh code — the customer is stuck"
    assert state(bot) == REGISTER_OTP

    bot.backend.route("POST", VERIFY_PHONE, lambda _c: {"data": {"verified": True}})
    await bot.send(user.text("654321"))
    assert payloads(bot, VERIFY_PHONE) == [{"otp": "654321"}]
    assert state(bot) is None


# ===========================================================================
# 4. Giving up, going back, starting over
# ===========================================================================


async def test_cancel_gets_the_customer_out_of_every_single_registration_state(make_bot):
    """/cancel is the escape hatch a stuck person reaches for. A state that
    swallows it leaves them typing into a conversation that answers nothing —
    the "frozen bot" reports this project has already had twice.
    """
    reach = {
        SELECT_LANGUAGE: reach_language_step,
        PHONE: reach_phone_step,
        REGISTER_OTP: reach_register_otp_step,
        LINK_ACCOUNT_CONFIRM: reach_link_confirm_step,
        LINK_ACCOUNT_OTP: reach_link_otp_step,
    }

    for expected_state, walk in reach.items():
        bot = await make_bot()
        user = bot.updates()
        assert await walk(bot, user) == expected_state, (
            f"the walk to {expected_state} no longer lands there"
        )

        bot.telegram.reset()
        await bot.send(user.command("/cancel"))

        assert state(bot) is None, f"/cancel did not exit state {expected_state}"
        assert "Cancelled. Here is the menu" in texts(bot), (
            f"/cancel in state {expected_state} showed the customer nothing: {texts(bot)}"
        )


async def test_start_after_giving_up_resumes_at_the_phone_step_and_does_not_register_twice(
    bot, user
):
    """Half the people who abandon signup come back and type /start again. The
    users row already exists by then but has no phone, so a second registration
    would either 409 on the unique telegram_id or mint a duplicate customer.
    They must land back on the phone step instead.
    """
    await reach_phone_step(bot, user)
    await bot.send(user.command("/cancel"))
    assert state(bot) is None

    bot.telegram.reset()
    await bot.send(user.command("/start"))

    assert state(bot) == PHONE, (
        "a customer with a row but no phone is not registered — dropping them "
        "at the main menu gives them a bot that cannot deliver anything"
    )
    assert "Share your contact to finish signing up" in texts(bot)
    assert has_contact_button(bot)
    assert len(posts(bot, REGISTER)) == 1, f"registered twice: {payloads(bot, REGISTER)}"

    await bot.send(user.contact(MY_PHONE_AS_TELEGRAM_SENDS_IT))
    assert bot.database.row["phone"] == MY_PHONE_E164
    assert state(bot) is None


async def test_start_from_a_fully_registered_customer_shows_the_menu_and_registers_nothing(
    make_bot,
):
    """The most common /start of all: an existing customer tapping the pinned
    Start button. It must never re-enter signup, and never re-POST registration.
    """
    bot = await make_bot(
        row={
            "id": 4242,
            "telegram_id": str(DEFAULT_USER_ID),
            "first_name": "Kamola",
            "phone": MY_PHONE_E164,
            "preferred_language": "ru",
            "role": "customer",
            "status": "active",
            "bot_state": "{}",
            "user_type": "individual",
        }
    )
    user = bot.updates()

    await bot.send(user.command("/start"))

    assert state(bot) is None
    assert "ru|С возвращением" in texts(bot), (
        "the greeting must follow the SAVED language, not the Telegram client "
        "locale — a customer who chose Russian on a Uzbek phone gets Uzbek "
        "otherwise, on every single /start"
    )
    assert posts(bot, REGISTER) == []


async def test_a_referral_deep_link_survives_a_failed_signup_and_is_resent_on_the_retry(
    bot, user
):
    """`t.me/bot?start=ref_CODE` is how the referral programme pays out. If a
    transient backend failure drops the code, the referrer is never credited
    and nobody finds out — the customer registers fine on the retry.
    """
    bot.backend.route("POST", REGISTER, lambda _c: backend_failure("db down", status_code=500))

    await bot.send(start_with_payload(user, "ref_KAMOLA7"))
    assert state(bot) == SELECT_LANGUAGE

    bot.telegram.reset()
    await bot.send(user.tap("set_language_uz"))

    assert payloads(bot, REGISTER) == [
        {
            "telegram_id": DEFAULT_USER_ID,
            "first_name": "Kamola",
            "last_name": None,
            "username": "kamola_test",
            "language_code": "uz",
            "referral_code": "KAMOLA7",
        }
    ]
    assert "Registration failed" in toasts(bot)
    assert "We could not sign you up. Contact support" in texts(bot)
    assert state(bot) is None, "a failed signup must not park the customer mid-flow"

    # The customer taps Start again a minute later.
    bot.backend.route("POST", REGISTER, _succeeding_register(bot))
    await bot.send(user.command("/start"))
    await bot.send(user.tap("set_language_uz"))

    assert payloads(bot, REGISTER)[-1].get("referral_code") == "KAMOLA7", (
        "the referral code was eaten by the failed attempt"
    )


def _succeeding_register(bot):
    def _responder(call):
        bot.database.row = {
            "id": 4242,
            "telegram_id": str(DEFAULT_USER_ID),
            "first_name": "Kamola",
            "phone": None,
            "preferred_language": (call.data or {}).get("language_code"),
            "role": "customer",
            "status": "active",
            "bot_state": "{}",
            "user_type": "individual",
        }
        return {"data": {"user": {"id": 4242}}}

    return _responder


# ===========================================================================
# 5. Stale buttons, double taps, and Telegram itself misbehaving
# ===========================================================================


async def test_every_button_the_registration_flow_renders_is_claimed_by_some_handler(
    bot, user
):
    """A tap that matches no handler shows a spinner and then nothing. Walk the
    flow and check, at each screen, that every button on the message the
    customer is looking at is claimed in the state they are actually parked in.
    """
    steps = [
        (user.command("/start"), "on the welcome screen"),
        (user.tap("set_language_uz"), "after choosing a language"),
    ]
    phone_belongs_to(bot)
    steps.append((user.contact(MY_PHONE_AS_TELEGRAM_SENDS_IT), "on the link offer"))

    for update, where in steps:
        bot.telegram.reset()
        await bot.send(update)

        shown = [c for c in bot.telegram.shown if c.text != KEYBOARD_CLEANUP_FILLER]
        assert shown, f"the bot showed nothing {where}"

        for data in shown[-1].callback_data():
            assert bot.handlers_matching(user.tap(data)), (
                f"the {data!r} button rendered {where} lands nowhere"
            )


async def test_a_stale_language_button_from_the_first_screen_cannot_re_register_the_customer(
    bot, user
):
    """The language message is deleted when the flow moves on, but Telegram
    keeps old messages tappable if the delete failed — and people scroll up.
    A late tap must not create a second customer or drag the flow backwards.
    """
    await reach_phone_step(bot, user, language="uz")
    assert len(posts(bot, REGISTER)) == 1

    bot.telegram.reset()
    await bot.send(user.tap("set_language_ru"))

    assert len(posts(bot, REGISTER)) == 1, (
        f"a stale language tap registered the customer again: {payloads(bot, REGISTER)}"
    )
    assert state(bot) == PHONE, "a stale tap must not rewind the conversation"


async def test_a_stale_link_yes_tap_after_the_merge_cannot_send_another_code(bot, user):
    """The link prompt stays in the chat history after the merge. Tapping Yes
    again must not spend another SMS, and must not restart a merge that has
    already happened.
    """
    await reach_link_otp_step(bot, user)
    bot.backend.route("POST", LINK_VERIFY, lambda _c: {"data": {"user": {"first_name": "K"}}})
    await bot.send(user.text("123456"))
    assert state(bot) is None
    otps_sent = len(posts(bot, LINK_SEND_OTP))

    await bot.send(user.tap("link_yes"))

    assert len(posts(bot, LINK_SEND_OTP)) == otps_sent, (
        "a stale link tap bought another SMS"
    )
    assert payloads(bot, LINK_VERIFY) == [
        {"telegram_id": DEFAULT_USER_ID, "otp": "123456"}
    ], "the merge must not be re-run by a tap on a message left in the history"

    # The tap used to be claimed by nothing but the debug logger — safe, in that
    # it bought no SMS, but the button then spun to the client timeout with
    # nothing said. `WaterBusinessBot._signup_step_expired` now answers it
    # honestly at group 0, and the live conversation still wins because
    # `link_account_confirm` is wrapped in `_consumes`.
    # tests/telegram_bot/test_menu_and_link_buttons_after_restart.py
    acting = [
        handler for _group, handler in bot.handlers_matching(user.tap("link_yes"))
    ]
    assert [getattr(h.callback, "__name__", "") for h in acting] == [
        "_signup_step_expired"
    ], "the stale tap must be answered, and by the expiry fallback alone"


async def test_a_telegram_rejection_while_answering_link_no_still_delivers_the_keyboard(
    bot, user
):
    """Was a RATCHET; the defect it pinned is fixed. Now the regression guard.

    `link_account_confirm` edits the prompt message before it sends the new
    contact keyboard. Telegram answers `editMessageText` with 400 "message is
    not modified" on a double tap and "message to edit not found" when the
    message has aged out — both appear in this project's production logs. The
    edit sat inside the handler's one big try/except, so the exception skipped
    the "share a different phone" message AND returned ConversationHandler.END:
    the customer was dropped out of signup with the last thing on screen being
    the link question, and nothing they typed afterwards was part of
    registration.

    WHAT THE FIX GUARANTEES: rewriting the link question is cosmetic and now
    goes through `_edit_or_replace_callback_message` (which treats "not
    modified" as the success it is). The share-phone keyboard — the actual step
    — is delivered either way, and the conversation stays in PHONE.
    """
    await reach_link_confirm_step(bot, user)
    bot.telegram.reset()
    bot.telegram.fail("editMessageText", "Bad Request: message is not modified")

    await bot.send(user.tap("link_no"))
    bot.telegram.clear_failures()

    assert state(bot) == PHONE, (
        "a refused cosmetic edit must not decide the conversation"
    )
    # The edit was REJECTED by Telegram, so the only thing that can have
    # reached the customer is a sendMessage — and the step depends on one.
    assert TRANSLATIONS["telegram.phone.share_phone_using_button"] in [
        call.text for call in bot.telegram.of("sendMessage")
    ], "the customer has to be told to share a different number"
    assert has_contact_button(bot), (
        "and given the button that does it, or signup is over for them"
    )

    # Proof the flow is genuinely alive rather than merely in the right state:
    # the next contact they share is still registration's to handle.
    bot.backend.route("POST", CHECK_PHONE, lambda _c: {"data": {"available": True}})
    await bot.send(user.contact(OTHER_PHONE_E164))

    assert state(bot) is None, "registration finished on the second number"
    assert texts(bot)[-1] == TRANSLATIONS[("uz", "telegram.registration_complete")]


async def test_a_failed_delete_of_the_language_message_does_not_stop_the_phone_step(bot, user):
    """The sibling case that IS handled: `language_selection` deletes the
    language message after sending the phone prompt, and wraps that delete in
    its own try/except. Losing that guard would fail every signup whose first
    message is older than Telegram's delete window.
    """
    await reach_language_step(bot, user)
    bot.telegram.reset()
    bot.telegram.fail("deleteMessage", "Bad Request: message to delete not found")

    await bot.send(user.tap("set_language_uz"))

    assert state(bot) == PHONE
    assert "uz|Telefon raqamingizni yuboring" in texts(bot)
    assert len(posts(bot, REGISTER)) == 1


# ===========================================================================
# 6. What the second handler group must NOT do with the same update
# ===========================================================================


async def test_neither_the_typed_phone_nor_the_typed_otp_reaches_the_support_inbox(bot, user):
    """The credential half of the double-dispatch leak, and the reason it was urgent.

    PTB runs at most one handler PER GROUP but walks every group. The
    registration conversation sits in group -2; the catch-all
    `MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text_message)`
    sits in group 0. So while nothing stopped dispatch, every text update in
    registration was handled twice, and the catch-all — finding no
    `awaiting_otp` flag and no `awaiting_input` state — silently POSTed it to
    `/api/v1/support/messages`.

    The result in production: the customer's phone number and their LIVE SMS
    one-time code were written into the admin support inbox as customer
    messages, and an operator saw two "tickets" from every person who signed
    up by typing. A one-time code sitting in a queue several staff accounts can
    read is a credential leak, not merely noise.

    WHAT THE FIX GUARANTEES: PHONE and REGISTER_OTP are registered through
    `WaterBusinessBot._consumes`, so both updates stop in group -2 while the
    conversation still advances on them exactly as before.
    """
    await reach_phone_step(bot, user)
    await bot.send(user.text(MY_PHONE_E164))
    assert state(bot) == REGISTER_OTP, "the conversation still consumed the phone"

    bot.backend.route("POST", VERIFY_PHONE, lambda _c: {"data": {"verified": True}})
    await bot.send(user.text("123456"))
    assert state(bot) is None, "and still consumed the code, finishing signup"

    assert posts(bot, SUPPORT_MESSAGES) == [], (
        "neither the phone number nor the live one-time code may be filed as "
        "a customer support message"
    )


async def test_the_language_tap_is_processed_by_the_conversation_alone(bot, user):
    """One tap, one handler — even though two are registered for the pattern.

    `^set_language_` is registered twice: inside the registration conversation
    (group -2, SELECT_LANGUAGE) and as a standalone callback handler in group 0
    (`language_handler.set_language`, which is what a settled customer uses to
    CHANGE language later). Both used to run for the same tap. The group-0 copy
    answers the callback query a second time — Telegram allows exactly one
    `answerCallbackQuery` per query, so the toast the customer actually saw was
    decided by a race — and, when the two handlers disagreed about the current
    language, it also edited a message the first handler had already deleted.
    A brand-new customer's very first tap was therefore answered by the
    language-CHANGE handler telling them they already used the language they
    had just picked for the first time.

    WHAT THE FIX GUARANTEES: the SELECT_LANGUAGE step goes through
    `WaterBusinessBot._consumes`, so registration owns the tap and dispatch
    stops in group -2. Both handlers still MATCH the tap — the group-0 one has
    to, for every customer who is NOT mid-signup — but only one runs.
    """
    await reach_language_step(bot, user)
    bot.telegram.reset()

    matched = bot.handlers_matching(user.tap("set_language_uz"))
    groups = sorted({group for group, _ in matched})

    assert groups == [-2, 0], (
        "both handlers still match the pattern; the fix is about which one "
        f"PTB reaches. Got {groups}."
    )

    await bot.send(user.tap("set_language_uz"))

    assert "\U0001F1FA\U0001F1FF you already use this language" not in toasts(bot), (
        "the language-CHANGE handler ran during signup and told a brand-new "
        f"customer they already use the language they just picked. Got: {toasts(bot)}"
    )
    assert state(bot) == PHONE, "registration is the handler that owns this tap"
    assert "uz|Telefon raqamingizni yuboring" in texts(bot), (
        "and it is registration's phone prompt the customer is left looking at"
    )
    assert toasts(bot) == [], (
        "PINNED, and a separate defect: registration's own `language_selection` "
        "never calls `query.answer()` on the new-customer path, so the button "
        "keeps spinning until Telegram times it out. Fixing that belongs in "
        "telegram_bot/handlers/profile.py; if it lands, expect registration's "
        "own toast here instead of the language-CHANGE handler's"
    )


async def test_a_registration_that_times_out_tells_the_customer_and_clears_the_flow(bot, user):
    """Was a RATCHET: the registration conversation used to expire in silence.

    It declares `conversation_timeout=300`. PTB does not announce that: when
    the timer fires it looks for handlers under the `ConversationHandler.TIMEOUT`
    key and, finding none, ends the flow without a word. Five minutes is well
    inside the time an Uzbek SMS can take to arrive, so the customer pasted
    their code into a conversation that no longer existed — and, because of the
    group-0 catch-all pinned above, the code was filed as a support ticket
    instead of verifying anything. From their side the bot simply stopped
    working half way through signing up.

    WHAT THE FIX GUARANTEES: a TIMEOUT state — a MessageHandler AND a
    CallbackQueryHandler, because the synthetic timeout update carries whatever
    the last real one was — that tells the customer the session expired, points
    them back at /start, and drops the flow's keys. `awaiting_otp` is the one
    that matters most: it is read by the group-0 text catch-all, so leaving it
    set sends the next thing this customer types to the phone-verification
    endpoint. (The in-conversation signup path deliberately does not set it —
    see the comment in `phone_text_received` — but the phone-verification flow
    does, and both share this user_data dict.)
    """
    handler = bot.conversation("registration")

    assert handler.conversation_timeout == 300, (
        "if the timeout is gone this test has nothing to guard"
    )
    timeout_handlers = handler.states.get(ConversationHandler.TIMEOUT)
    assert timeout_handlers, (
        "registration has no ConversationHandler.TIMEOUT state, so PTB would "
        "end the flow in silence"
    )
    kinds = {type(inner) for inner in timeout_handlers}
    assert MessageHandler in kinds, (
        "a customer parked on the typed-phone or OTP step produces a "
        "message-shaped timeout update and would expire unhandled"
    )
    assert CallbackQueryHandler in kinds, (
        "a customer parked on the language step produces a callback-shaped "
        "timeout update and would expire unhandled"
    )

    # The states that DO exist, so a renumbering or a dropped branch is loud.
    assert sorted(state for state in handler.states if state != ConversationHandler.TIMEOUT) == sorted(
        [SELECT_LANGUAGE, PHONE, LINK_ACCOUNT_CONFIRM, LINK_ACCOUNT_OTP, REGISTER_OTP]
    )

    await reach_register_otp_step(bot, user)
    assert state(bot) == REGISTER_OTP, "the customer is waiting for their SMS"
    flow_data = bot.application.user_data[user.user_id]
    assert flow_data.get("pending_phone_verification"), (
        "the half-finished signup this timeout has to clear"
    )
    bot.telegram.reset()

    # Their last real update was the typed phone number, so that is the shape
    # PTB hands the TIMEOUT state.
    result = await fire_conversation_timeout(bot, "registration", user.text(MY_PHONE_E164))

    assert result == ConversationHandler.END
    assert "uz|Ro'yxatdan o'tish vaqti tugadi. Qaytadan boshlash uchun /start yuboring" in texts(bot), (
        f"the customer was told nothing when their signup expired: {texts(bot)}"
    )
    for key in ("awaiting_otp", "otp_prompted_update_id", "pending_phone_verification",
                "pending_phone", "pending_link_phone"):
        assert key not in flow_data, (
            f"{key!r} survived the timeout; the next thing this customer types "
            "would be treated as an OTP by the group-0 catch-all"
        )


async def test_a_timed_out_registration_does_not_swallow_the_next_message_as_an_otp(bot, user):
    """The customer-visible half of the timeout fix, driven end to end.

    `awaiting_otp` lives in `user_data`, not in the conversation, so it
    outlives the conversation that set it. Before the TIMEOUT state existed,
    the next sentence this customer typed was verified as a one-time code
    against `/auth/verify-phone` — for a half-registered customer with no
    token, that is an error message about a code they never sent.
    """
    await reach_register_otp_step(bot, user)
    await fire_conversation_timeout(bot, "registration", user.text(MY_PHONE_E164))
    bot.telegram.reset()

    await bot.send(user.text("salom, nima bo'ldi?"))

    assert not [c for c in bot.backend.calls if c.endpoint == VERIFY_PHONE], (
        "the message after a timed-out signup was verified as an OTP"
    )
