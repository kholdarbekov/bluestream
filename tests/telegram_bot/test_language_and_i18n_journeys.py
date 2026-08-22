"""What the customer READS, driven through the real dispatcher.

WHY THIS FILE EXISTS
--------------------
Language is where this project has repeatedly shipped user-visible breakage,
and every one of those defects lived in a seam no handler-level test can see:

* a reply-keyboard label regex compiled from the ENGLISH copy only, so Cancel
  was dead in uz and ru and the tap escaped the conversation entirely
  (``telegram_bot/bot.py::_resolve_tapped_label`` exists because of that);
* ``preferred_language`` stored raw from Telegram's ``language_code`` — an IETF
  tag like ``ru-RU`` — which missed the lookup in :meth:`i18n.Translation.get`
  and silently served the FALLBACK language to a Russian speaker
  (``normalize_language`` exists because of that);
* a translation row that was never seeded, rendering as a humanised key instead
  of the copy — and, if that fallback ever became the empty string, as a button
  Telegram refuses to accept at all.

``tests/telegram_bot/test_i18n_translation.py`` checks :class:`Translation` in
isolation and ``test_address_location_patterns.py`` checks the label matcher in
isolation. Neither can answer the question a customer asks: *is the screen in
front of me in my language, and does the button on it do anything?* Everything
below goes in through ``Application.process_update``, so the conversation state
machine, the handler groups, the label matcher and the real keyboards are all
in the loop.

THE COUPLING THIS FILE LEANS ON
-------------------------------
``_resolve_tapped_label`` calls ``i18n.get`` WHEN THE TAP ARRIVES, off the same
translation table the keyboard builder renders from. So the table a test
installs is the table the router matches against for as long as it stays
installed — and a test may change it mid-flight and expect the change to take
effect on the very next update. Both halves are asserted, not assumed: see
``test_the_label_matcher_reads_the_table_the_harness_installed`` and
``test_copy_reseeded_after_startup_matches_the_moment_it_renders``.

RATCHET TESTS
-------------
Four tests below pin CURRENT behaviour that is wrong — including a real hole in
``Translation.get``'s interpolation guard that ends the address flow in total
silence. Each says so in its docstring and names the behaviour that would be
correct. They must be inverted, not deleted, when the defect is fixed.
"""

from __future__ import annotations

import pytest

# Module-level, so `config`, `i18n`, `bot` and `handlers` resolve as the BOT's
# versions before anything below touches them. See tests/telegram_bot/conftest.py.
from config import config
from i18n import Translation, i18n as i18n_singleton
from handlers import callback_dedup
from handlers.profile import ADDRESS_LOCATION, ADDRESS_REGION, ADDRESS_TITLE

from tests.telegram_bot.ptb_harness import (
    FakeDatabase,
    backend_failure,
    build_bot_harness,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


UZ, RU, EN = "uz", "ru", "en"

# Read from production config rather than hard-coded: adding a language must
# make the "does this button work in every language?" tests cover it, not
# quietly skip it.
SUPPORTED = list(config.localization.supported_languages)

# What a customer with no usable `preferred_language` ends up reading. Also
# read from config, because it is deployment-set (`DEFAULT_LANGUAGE`) and a
# literal here would make this file pass on a laptop and fail on the Pi.
DEFAULT_LANGUAGE = config.localization.default_language

# The pin from the traced production session; inside TASHKENT_POLYGON so the
# real delivery-zone SSOT accepts it.
PIN_LAT = 41.32354
PIN_LNG = 69.241036


# ---------------------------------------------------------------------------
# The copy, exactly as scripts/seed_backend_translations.py seeds it
# ---------------------------------------------------------------------------
#
# Verbatim on purpose. A test that invents its own strings can prove a screen
# rendered SOMETHING in the right language; only the real copy can prove the
# label a customer taps is the label the dispatcher matches.

SEEDED = {
    "telegram.main_menu": {
        EN: "💧 Welcome to Aqua Element! Choose an option below to get started.",
        UZ: "💧 Aqua Elementga xush kelibsiz! Boshlash uchun quyidagi bo'limlardan birini tanlang.",
        RU: "💧 Добро пожаловать в Aqua Element! Выберите нужный раздел ниже, чтобы начать.",
    },
    "telegram.menu.products": {
        EN: "💧 Order Water", UZ: "💧 Suv buyurtma berish", RU: "💧 Заказать воду",
    },
    "telegram.menu.orders": {
        EN: "📦 My Orders", UZ: "📦 Buyurtmalarim", RU: "📦 Мои заказы",
    },
    "telegram.menu.profile": {
        EN: "👤 My Profile", UZ: "👤 Profilim", RU: "👤 Мой профиль",
    },
    "telegram.menu.support": {
        EN: "🆘 Get Help", UZ: "🆘 Yordam", RU: "🆘 Помощь",
    },
    "telegram.menu.language": {
        EN: "🌐 Language", UZ: "🌐 Til", RU: "🌐 Язык",
    },
    "telegram.menu.subscriptions": {
        EN: "🔄 Auto-Delivery", UZ: "🔄 Avto-yetkazib berish", RU: "🔄 Автодоставка",
    },
    "telegram.menu.loyalty": {EN: "🎁 Aqua Club", UZ: "🎁 Aqua Club", RU: "🎁 Aqua Club"},
    "telegram.cart_title": {EN: "🛒 My Cart", UZ: "🛒 Savatim", RU: "🛒 Моя корзина"},
    "telegram.language.current": {
        EN: "Current language", UZ: "Joriy til", RU: "Текущий язык",
    },
    "telegram.language.select_prompt": {
        EN: "Choose your language:", UZ: "Tilingizni tanlang:", RU: "Выберите язык:",
    },
    "telegram.language.changed_success": {
        EN: "Language changed", UZ: "Til o'zgartirildi", RU: "Язык изменён",
    },
    "telegram.language.confirmation_title": {
        EN: "Language updated", UZ: "Til yangilandi", RU: "Язык обновлён",
    },
    "telegram.language.confirmation_message": {
        EN: "All menus and messages will now appear in your new language. Enjoy Aqua Element!",
        UZ: "Barcha menyular va xabarlar endi yangi tilingizda ko'rsatiladi. Aqua Element xizmatidan zavqlaning!",
        RU: "Теперь все меню и сообщения будут на новом языке. Приятного пользования Aqua Element!",
    },
    "telegram.language.now_using": {
        EN: "You're now using {language}",
        UZ: "Endi {language} tilidan foydalanyapsiz",
        RU: "Теперь вы используете язык {language}",
    },
    "telegram.language.already_selected": {
        EN: "You're already using this language.",
        UZ: "Siz allaqachon shu tildan foydalanyapsiz.",
        RU: "Вы уже используете этот язык.",
    },
    "telegram.language.invalid_selection": {
        EN: "This language isn't available. Please choose another.",
        UZ: "Bu til mavjud emas. Iltimos, boshqasini tanlang.",
        RU: "Этот язык недоступен. Пожалуйста, выберите другой.",
    },
    "telegram.cancel": {EN: "❌ Cancel", UZ: "❌ Bekor qilish", RU: "❌ Отмена"},
    "telegram.action_cancelled": {
        EN: "❌ Action cancelled.",
        UZ: "❌ Amal bekor qilindi.",
        RU: "❌ Действие отменено.",
    },
    "telegram.action_cancelled_short": {
        EN: "❌ Cancelled", UZ: "❌ Bekor qilindi", RU: "❌ Отменено",
    },
    "telegram.address.share_location_button": {
        EN: "📍 Share Location",
        UZ: "📍 Joylashuvni ulashish",
        RU: "📍 Поделиться местоположением",
    },
    "telegram.address.enter_manually_button": {
        EN: "✏️ Enter Manually", UZ: "✏️ Qo'lda kiritish", RU: "✏️ Ввести вручную",
    },
    "telegram.address.reenter_manually_button": {
        EN: "✏️ Re-enter Address",
        UZ: "✏️ Manzilni qayta kiritish",
        RU: "✏️ Ввести адрес заново",
    },
    "telegram.address.manual_entry_started": {
        EN: "✏️ Manual address entry",
        UZ: "✏️ Manzilni qo'lda kiritish",
        RU: "✏️ Ручной ввод адреса",
    },
    "telegram.address.select_region": {
        EN: "Please select your region:",
        UZ: "Iltimos, viloyatingizni tanlang:",
        RU: "Пожалуйста, выберите ваш регион:",
    },
    "telegram.address.location_received": {
        EN: "📍 Location received!",
        UZ: "📍 Joylashuv qabul qilindi!",
        RU: "📍 Местоположение получено!",
    },
    "telegram.address.title_prompt": {
        EN: "Great! Now give this address a name.",
        UZ: "Ajoyib! Endi bu manzilga nom bering.",
        RU: "Отлично! Теперь дайте этому адресу название.",
    },
    "telegram.address.detected_location_prefix": {
        EN: "📍 *Detected location:*\n{address}\n\n",
        UZ: "📍 *Aniqlangan joylashuv:*\n{address}\n\n",
        RU: "📍 *Определенное местоположение:*\n{address}\n\n",
    },
    "telegram.address.location_prompt_enhanced": {
        EN: "📍 *Add New Address*\n\nPlease share your location.",
        UZ: "📍 *Yangi manzil qo'shish*\n\nJoylashuvingizni ulashing.",
        RU: "📍 *Добавить новый адрес*\n\nПоделитесь своим местоположением.",
    },
}

TRANSLATIONS = {
    (language, key): value
    for key, per_language in SEEDED.items()
    for language, value in per_language.items()
}

# The reverse-geocoded street the default FakeBackend returns.
GEOCODED = "15, Chilonzor dahasi, Toshkent shahri"


def copy_in(key: str, language: str) -> str:
    """The seeded string for ``key`` in ``language``.

    Fails loudly rather than falling back, so a language added to
    ``config.localization.supported_languages`` without copy in this table
    surfaces as "you must seed this", not as a silently skipped assertion.
    """
    per_language = SEEDED[key]
    assert language in per_language, (
        f"{key!r} has no {language!r} copy in this test's table. "
        f"{language!r} is in config.localization.supported_languages, so every "
        "localized-button test below is meaningless for it until copy is added."
    )
    return per_language[language]


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------


class LanguageAwareDatabase(FakeDatabase):
    """A users table that honours the one UPDATE the language switch writes.

    ``BotUserRepository.update_user_language`` issues

        UPDATE users SET preferred_language = $1 ... WHERE telegram_id = $2

    and the base fake records the statement and drops it. That makes the only
    question worth asking about a language switch — *does the NEXT screen come
    back in the new language?* — unanswerable, because ``get_user_language``
    would keep replying with the old value no matter what the handler did.

    This models one column of one table. It re-implements no production logic:
    the SQL, the parameter order and the value all come from the bot.
    """

    async def execute(self, query, *args):
        result = await super().execute(query, *args)
        if "SET preferred_language" in query and args:
            self.user["preferred_language"] = args[0]
        return result


def customer_speaking(language: str) -> LanguageAwareDatabase:
    """A registered customer whose stored ``preferred_language`` is ``language``.

    Passed RAW — including deliberately malformed values like ``ru-RU`` — because
    that column really does hold whatever Telegram or ``sync-profile`` last put
    in it, unvalidated.
    """
    database = LanguageAwareDatabase()
    database.user["preferred_language"] = language
    return database


async def bot_for(monkeypatch, language: str, translations=TRANSLATIONS):
    return await build_bot_harness(
        monkeypatch,
        translations=translations,
        database=customer_speaking(language),
    )


@pytest.fixture
async def uz_bot(monkeypatch):
    """The common case: an Uzbek-speaking customer on a fully seeded bot."""
    return await bot_for(monkeypatch, UZ)


def toasts(bot) -> list[str]:
    """Every answerCallbackQuery that carried visible text."""
    return [
        call.params["text"]
        for call in bot.telegram.of("answerCallbackQuery")
        if call.params.get("text")
    ]


def profile_syncs(bot) -> list[dict]:
    """The payloads sent to the backend's profile endpoint, in order."""
    return [
        call.data
        for call in bot.backend.calls
        if call.method == "PUT" and call.endpoint == "/api/v1/auth/profile"
    ]


def age_dedup_locks() -> None:
    """Push every in-memory dedup lock past its TTL.

    The callback-dedup middleware debounces (user_id, callback_data) for two
    REAL seconds, and since 2026-08-21 the harness carries it because
    production registers it in ``_setup_handlers()``. A journey that
    deliberately taps the same button twice — a customer switching language,
    changing their mind, switching back — must age the lock table, or the
    second tap is dropped before any handler runs and the test silently
    asserts nothing. Sleeping would put two real seconds in the suite.
    """
    stale = callback_dedup.time.monotonic() - callback_dedup._DEDUP_TTL_SECONDS - 1
    for key in list(callback_dedup._in_memory_locks):
        callback_dedup._in_memory_locks[key] = stale


async def open_address_flow(bot, user):
    """Tap "Add address" and land on the location prompt."""
    await bot.send(user.tap("add_new_address"))
    return bot.conversation_state("address_conversation")


# ===========================================================================
# Switching language from the menu
# ===========================================================================


async def test_switching_language_confirms_in_the_language_just_chosen(uz_bot):
    """The confirmation for "I chose Russian" has to BE in Russian.

    Confirming a language change in the language the customer just left is the
    single most obvious way for this screen to be wrong, and it is invisible to
    any test that stubs `i18n.get` to echo its key.
    """
    user = uz_bot.updates()

    await uz_bot.send(user.tap("menu_language"))
    menu = uz_bot.telegram.last_shown()
    assert copy_in("telegram.language.select_prompt", UZ) in menu.text, (
        "the language menu itself must be in the customer's CURRENT language"
    )
    assert "set_language_ru" in menu.callback_data()

    uz_bot.telegram.reset()
    await uz_bot.send(user.tap("set_language_ru"))

    confirmation = uz_bot.telegram.last_shown()
    assert copy_in("telegram.language.confirmation_title", RU) in confirmation.text
    assert copy_in("telegram.language.confirmation_message", RU) in confirmation.text
    assert copy_in("telegram.language.confirmation_title", UZ) not in confirmation.text, (
        "the confirmation is still being rendered in the OLD language"
    )
    assert copy_in("telegram.menu.products", RU) in confirmation.button_labels(), (
        "the menu keyboard attached to the confirmation must switch too — a "
        "Russian message under Uzbek buttons is the same bug, half-fixed"
    )
    assert any(copy_in("telegram.language.changed_success", RU) in text for text in toasts(uz_bot))


async def test_the_next_screen_after_a_language_switch_reads_in_the_new_language(uz_bot):
    """The real regression: the switch must be PERSISTED, not just rendered once.

    A handler that builds the confirmation from its own local `language_code`
    looks perfect in a screenshot and reverts on the very next tap, because
    every other screen re-reads `preferred_language` from the database.
    """
    user = uz_bot.updates()
    await uz_bot.send(user.tap("menu_language"))
    await uz_bot.send(user.tap("set_language_ru"))

    assert uz_bot.database.user["preferred_language"] == "ru", (
        "the bot's own users row was never updated"
    )
    assert profile_syncs(uz_bot) == [{"preferred_language": "ru"}], (
        "the backend must be told exactly once, with exactly this payload — it "
        "is what renders the customer's emails and web pages"
    )

    uz_bot.telegram.reset()
    await uz_bot.send(user.tap("back_to_main"))

    main_menu = uz_bot.telegram.last_shown()
    assert main_menu.text == copy_in("telegram.main_menu", RU)
    labels = main_menu.button_labels()
    assert copy_in("telegram.menu.orders", RU) in labels
    assert copy_in("telegram.menu.orders", UZ) not in labels, (
        "a screen mixing both languages means one of them is coming from a "
        "stale cached value"
    )


async def test_choosing_the_language_already_in_use_writes_nothing_anywhere(uz_bot):
    """Re-picking the current language is a no-op with a toast.

    Falling through would burn a database write, a backend round trip and a
    full message edit to change nothing — and, worse, the edit would be a
    "message is not modified" rejection, which this bot's logs are already full
    of.
    """
    user = uz_bot.updates()
    await uz_bot.send(user.tap("menu_language"))

    backend_calls_before = len(uz_bot.backend.calls)
    uz_bot.telegram.reset()
    await uz_bot.send(user.tap("set_language_uz"))

    assert any(
        copy_in("telegram.language.already_selected", UZ) in text for text in toasts(uz_bot)
    ), "the customer must be told why nothing happened, in their own language"
    assert uz_bot.telegram.of("editMessageText") == [], "nothing should have been re-rendered"
    assert len(uz_bot.backend.calls) == backend_calls_before
    assert uz_bot.database.user["preferred_language"] == "uz"


async def test_tapping_russian_when_the_stored_value_is_ru_RU_counts_as_no_change(monkeypatch):
    """`ru-RU` and `ru` are the same language to the customer.

    `set_language` compares the tapped code against `get_user_language()`, which
    normalizes. Drop that normalization and a `ru-RU` customer tapping Russian
    is treated as a real change: a pointless write, a pointless backend sync and
    a "language updated" screen for a language they were already reading.
    """
    bot = await bot_for(monkeypatch, "ru-RU")
    user = bot.updates()
    await bot.send(user.tap("menu_language"))

    bot.telegram.reset()
    await bot.send(user.tap("set_language_ru"))

    assert any(
        copy_in("telegram.language.already_selected", RU) in text for text in toasts(bot)
    )
    assert profile_syncs(bot) == [], "no sync should have been sent for a non-change"
    assert bot.database.user["preferred_language"] == "ru-RU", (
        "the stored value is left exactly as it was"
    )


async def test_a_stale_button_for_a_language_the_bot_dropped_is_refused_politely(uz_bot):
    """Telegram keeps old inline keyboards alive forever.

    A customer scrolling back to a message from a build that offered Kazakh can
    still tap it. `^set_language_` matches, so the guard against unsupported
    codes is the only thing between that tap and a `preferred_language` no
    screen in the bot can render.
    """
    user = uz_bot.updates()

    await uz_bot.send(user.tap("set_language_kk"))

    assert any(
        copy_in("telegram.language.invalid_selection", UZ) in text for text in toasts(uz_bot)
    ), "the refusal must be in the language the customer is currently reading"
    assert uz_bot.database.user["preferred_language"] == "uz", (
        "an unsupported code must never reach the users row"
    )
    assert profile_syncs(uz_bot) == []


async def test_a_backend_outage_still_changes_the_language_the_bot_renders(uz_bot):
    """The backend sync is best-effort; the bot-side change is not.

    Letting a 500 on `PUT /api/v1/auth/profile` abort the switch would mean the
    customer taps Russian, sees an error, and keeps reading Uzbek — for a
    failure in a system that has nothing to do with the bot's own rendering.
    """
    uz_bot.backend.route(
        "PUT",
        "/api/v1/auth/profile",
        lambda _call: backend_failure("profile service unavailable", status_code=500),
    )
    user = uz_bot.updates()
    await uz_bot.send(user.tap("menu_language"))
    await uz_bot.send(user.tap("set_language_ru"))

    assert uz_bot.database.user["preferred_language"] == "ru"
    assert copy_in("telegram.language.confirmation_title", RU) in uz_bot.telegram.last_shown().text

    uz_bot.telegram.reset()
    await uz_bot.send(user.tap("back_to_main"))
    assert uz_bot.telegram.last_shown().text == copy_in("telegram.main_menu", RU)


async def test_a_customer_who_tries_russian_and_changes_their_mind_gets_uzbek_back(uz_bot):
    """Trying a language is a normal thing to do, and it has to be reversible.

    The second visit to the language menu is a DELIBERATE re-tap of the same
    button, seconds later, so the dedup window is aged first — otherwise the
    guard eats it and the rest of this journey never runs.
    """
    user = uz_bot.updates()
    await uz_bot.send(user.tap("menu_language"))
    await uz_bot.send(user.tap("set_language_ru"))
    assert uz_bot.database.user["preferred_language"] == "ru"

    age_dedup_locks()
    await uz_bot.send(user.tap("menu_language"))
    russian_menu = uz_bot.telegram.last_shown()
    assert copy_in("telegram.language.select_prompt", RU) in russian_menu.text, (
        "the way back has to be reachable in the language they just switched to"
    )

    uz_bot.telegram.reset()
    await uz_bot.send(user.tap("set_language_uz"))

    assert uz_bot.database.user["preferred_language"] == "uz"
    assert profile_syncs(uz_bot) == [
        {"preferred_language": "ru"},
        {"preferred_language": "uz"},
    ], "the backend has to see both moves, in order"
    assert copy_in("telegram.language.confirmation_title", UZ) in uz_bot.telegram.last_shown().text

    uz_bot.telegram.reset()
    await uz_bot.send(user.tap("back_to_main"))
    assert uz_bot.telegram.last_shown().text == copy_in("telegram.main_menu", UZ)


async def test_an_impatient_double_tap_on_a_language_button_syncs_once(uz_bot):
    """The language button acks late, so people tap it twice.

    The second tap would arrive AFTER the row already says `ru`, so
    `set_language` would take its "already using this" branch and replace the
    confirmation screen with a bare toast — the customer's screen would flicker
    back to the language menu for no reason. The dedup guard at group -5 is what
    stops that.
    """
    user = uz_bot.updates()
    await uz_bot.send(user.tap("menu_language"))

    await uz_bot.send(user.tap("set_language_ru"))
    duplicate = user.tap("set_language_ru")
    await uz_bot.send(duplicate)

    assert profile_syncs(uz_bot) == [{"preferred_language": "ru"}], (
        "the duplicate tap reached the handler"
    )
    # The discriminating assertion. Without the guard the second tap DOES reach
    # `set_language`, finds the row already saying `ru`, and takes the
    # "already using this language" branch — so the customer's reward for
    # tapping twice is a toast telling them off for a change they did make.
    assert not any(
        copy_in("telegram.language.already_selected", RU) in text for text in toasts(uz_bot)
    ), "the duplicate reached the handler and was answered as a no-op"
    acked = {
        call.params["callback_query_id"] for call in uz_bot.telegram.of("answerCallbackQuery")
    }
    assert duplicate.callback_query.id in acked, (
        "the dropped duplicate still has to be answered, or its spinner never stops"
    )
    assert copy_in("telegram.language.confirmation_title", RU) in uz_bot.telegram.last_shown().text


# ===========================================================================
# What is actually stored in preferred_language
# ===========================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    "stored,expected",
    [
        ("uz", "uz"),
        ("ru", "ru"),
        ("ru-RU", "ru"),           # Telegram's own language_code, written raw
        ("ru_RU", "ru"),           # the same tag with an underscore separator
        ("RU", "ru"),              # a shouted code from a bulk import
        ("  Uz-Latn-UZ  ", "uz"),  # a full BCP-47 tag, padded
        ("en-GB", "en"),
        ("russian", "ru"),         # the documented alias
        ("", DEFAULT_LANGUAGE),
        (None, DEFAULT_LANGUAGE),
        ("klingon", DEFAULT_LANGUAGE),
    ],
)
def test_normalize_language_maps_every_shape_that_reaches_the_column(stored, expected):
    """`preferred_language` is written raw from Telegram AND from
    `POST /api/v1/auth/sync-profile`, neither of which validates it. Every value
    below has a plausible route into that column; each must land on a code the
    translation table is actually keyed by, or `get()` serves the fallback
    language and reports success."""
    assert Translation().normalize_language(stored) == expected


async def test_a_customer_stored_as_ru_RU_reads_russian_not_the_fallback(monkeypatch):
    """The whole point of `normalize_language`, measured at the screen.

    Without it `get('telegram.main_menu', 'ru-RU')` misses the `ru` table,
    silently falls through to the FALLBACK language, and a Russian speaker reads
    English — with nothing in the logs to say anything went wrong.
    """
    bot = await bot_for(monkeypatch, "ru-RU")
    user = bot.updates()

    await bot.send(user.tap("back_to_main"))

    menu = bot.telegram.last_shown()
    assert menu.text == copy_in("telegram.main_menu", RU)
    assert copy_in("telegram.menu.products", RU) in menu.button_labels()
    assert copy_in("telegram.menu.products", EN) not in menu.button_labels()


async def test_a_junk_preferred_language_falls_back_to_the_configured_default(monkeypatch):
    """An unparseable value must degrade to the DEPLOYED default language.

    Not to English-because-that-is-the-dataclass-default: `DEFAULT_LANGUAGE` is
    an environment knob precisely so this bot can serve Uzbek speakers, and a
    hard-coded English fallback here would put every corrupted row in front of
    the wrong copy.
    """
    bot = await bot_for(monkeypatch, "klingon")
    user = bot.updates()

    await bot.send(user.tap("back_to_main"))

    assert bot.telegram.last_shown().text == copy_in("telegram.main_menu", DEFAULT_LANGUAGE)


async def test_the_language_menu_ticks_the_language_an_ietf_tag_resolves_to(monkeypatch):
    """The ✅ marker is how the customer knows which language they are on.

    `LanguageKeyboards.language_selection` compares each code against the
    CURRENT language, so an un-normalized `ru-RU` would tick nothing at all —
    three unmarked buttons, and the customer's only way to find out is to tap
    one.
    """
    bot = await bot_for(monkeypatch, "ru-RU")
    user = bot.updates()

    await bot.send(user.tap("menu_language"))

    labels = bot.telegram.last_shown().button_labels()
    ticked = [label for label in labels if label.startswith("✅")]
    assert len(ticked) == 1, f"exactly one language must be marked current, got {labels}"
    assert i18n_singleton.get_language_name(RU, RU) in ticked[0], (
        f"the tick is on the wrong language: {ticked[0]!r}"
    )
    assert copy_in("telegram.language.current", RU) in bot.telegram.last_shown().text


# ===========================================================================
# Reply-keyboard labels — the buttons matched by a compiled regex
# ===========================================================================


@pytest.mark.parametrize("language", SUPPORTED)
async def test_enter_manually_opens_manual_entry_in_every_supported_language(
    monkeypatch, language
):
    """A reply-keyboard tap arrives as ORDINARY TEXT.

    The only thing connecting it to `skip_location_sharing` is a regex compiled
    from the localized label. When that regex was built from an English word,
    this button was dead for every uz and ru customer and their tap fell through
    to the group-0 catch-all, which files free text as a support ticket and
    answers nothing.
    """
    bot = await bot_for(monkeypatch, language)
    user = bot.updates()

    assert await open_address_flow(bot, user) == ADDRESS_LOCATION
    label = copy_in("telegram.address.enter_manually_button", language)
    assert label in bot.telegram.last_shown().button_labels(), (
        "the customer is not even being shown this button in their language"
    )

    bot.telegram.reset()
    await bot.send(user.text(label))

    assert bot.conversation_state("address_conversation") == ADDRESS_REGION, (
        f"the {language!r} 'Enter manually' button is dead: the tap did not "
        "reach skip_location_sharing"
    )
    shown = bot.telegram.texts()
    assert copy_in("telegram.address.manual_entry_started", language) in shown
    assert copy_in("telegram.address.select_region", language) in shown


@pytest.mark.parametrize("language", SUPPORTED)
async def test_cancel_ends_the_address_flow_in_every_supported_language(monkeypatch, language):
    """The defect this file is named after.

    `telegram.cancel` is seeded as "❌ Bekor qilish" / "❌ Отмена" / "❌ Cancel".
    The pattern that preceded `_resolve_tapped_label` matched the English word
    only, so Cancel did nothing in uz and ru — the customer was stuck in the
    address flow with a Cancel button that answered with silence.

    The Cancel row rides the same reply keyboard the zero-address checkout and
    the geocode-retry prompt arm (`orders.py`, `profile.py::retry_geocode`), and
    Telegram keeps a reply keyboard on screen until something replaces it.
    """
    bot = await bot_for(monkeypatch, language)
    user = bot.updates()
    assert await open_address_flow(bot, user) == ADDRESS_LOCATION

    bot.telegram.reset()
    await bot.send(user.text(copy_in("telegram.cancel", language)))

    assert bot.conversation_state("address_conversation") is None, (
        f"the {language!r} Cancel button is dead: the customer is still trapped "
        "in the address flow"
    )
    shown = bot.telegram.shown
    dismissal = next(
        (
            call
            for call in shown
            if call.text == copy_in("telegram.action_cancelled_short", language)
        ),
        None,
    )
    assert dismissal is not None, (
        f"the customer was never told the flow was cancelled; they saw "
        f"{[call.text for call in shown]}"
    )
    assert dismissal.reply_markup.get("remove_keyboard") is True, (
        "the location keyboard must be taken away with the flow, or the "
        "customer is left holding buttons that now lead nowhere"
    )
    # Located by its own text rather than by position: PTB runs one handler per
    # GROUP, so this same text is ALSO delivered to the group-0 catch-all
    # (`_handle_text_message`), which appends a message of its own. Asserting on
    # `shown[-1]` would be asserting on that unrelated handler.
    landing = next(
        (call for call in shown if call.text == copy_in("telegram.action_cancelled", language)),
        None,
    )
    assert landing is not None, (
        f"cancelling must say so; the customer saw {[call.text for call in shown]}"
    )
    assert copy_in("telegram.menu.products", language) in landing.button_labels(), (
        "cancelling must land the customer on a usable main menu"
    )


async def test_a_cancel_label_left_over_from_the_previous_language_still_cancels(monkeypatch):
    """A customer changes language WHILE an address flow is open.

    Telegram reply keyboards are client-side and survive unrelated messages, so
    the Uzbek "❌ Bekor qilish" is still sitting on this customer's screen after
    they switch to Russian. `_resolve_tapped_label` sweeps every supported
    language on every tap precisely so the stale button keeps working — and the
    reply they get must be in their NEW language, because that is the one the
    handler re-reads from the database.
    """
    bot = await bot_for(monkeypatch, UZ)
    user = bot.updates()
    assert await open_address_flow(bot, user) == ADDRESS_LOCATION
    assert copy_in("telegram.cancel", UZ) != copy_in("telegram.cancel", RU)

    # The customer reaches for /language mid-flow; the address conversation has
    # no handler for it, so it is served by the top-level command handler and
    # the flow stays open underneath.
    await bot.send(user.command("language"))
    await bot.send(user.tap("set_language_ru"))
    assert bot.conversation_state("address_conversation") == ADDRESS_LOCATION, (
        "switching language must not silently destroy the flow in progress"
    )
    assert bot.database.user["preferred_language"] == "ru"

    bot.telegram.reset()
    await bot.send(user.text(copy_in("telegram.cancel", UZ)))

    assert bot.conversation_state("address_conversation") is None, (
        "the stale Uzbek Cancel button must still cancel"
    )
    assert copy_in("telegram.action_cancelled", RU) in bot.telegram.texts(), (
        "the reply belongs in the language the customer just switched TO"
    )
    assert copy_in("telegram.action_cancelled", UZ) not in bot.telegram.texts()


async def test_an_ordinary_sentence_about_cancelling_is_not_the_cancel_button(monkeypatch):
    """A tap is the WHOLE string, and that is load-bearing.

    The substring pattern this replaced would have matched any message
    containing the word, so a customer typing "I don't want to cancel" mid-flow
    would have had their address flow cancelled by the bot agreeing with them.
    Its successor `_label_pattern` then allowed one leading token, so a note
    that merely ENDED in the copy did the same thing; `_resolve_tapped_label`
    strips only an emoji decoration a keyboard row could have carried.
    """
    bot = await bot_for(monkeypatch, RU)
    user = bot.updates()
    assert await open_address_flow(bot, user) == ADDRESS_LOCATION

    await bot.send(user.text("Я не хочу ❌ Отмена заказа, просто вопрос"))

    assert bot.conversation_state("address_conversation") == ADDRESS_LOCATION, (
        "a sentence merely containing the Cancel copy must not cancel the flow"
    )


async def test_the_label_matcher_reads_the_table_the_harness_installed(monkeypatch):
    """The matcher resolves against the LIVE translation table, not a seed file.

    Proved with copy that exists nowhere in the seed script, so the only way
    this tap can land is if `_resolve_tapped_label` read the table this test
    supplied. (This used to assert an ORDERING — the table had to be installed
    before `_setup_handlers()` compiled the regexes. It no longer has to be:
    the lookup happens when the tap arrives. What survives is the half that
    matters, that house copy routes; the mid-flight case is its twin below.)
    """
    house_copy = "🚫 To'xtatish"
    table = dict(TRANSLATIONS)
    table[(UZ, "telegram.cancel")] = house_copy

    bot = await build_bot_harness(
        monkeypatch, translations=table, database=customer_speaking(UZ)
    )
    user = bot.updates()
    assert await open_address_flow(bot, user) == ADDRESS_LOCATION

    await bot.send(user.text(house_copy))

    assert bot.conversation_state("address_conversation") is None
    assert copy_in("telegram.action_cancelled", UZ) in bot.telegram.texts()


async def test_copy_reseeded_after_startup_matches_the_moment_it_renders(monkeypatch):
    """The inverted RATCHET: a mid-day reseed no longer costs the customer the button.

    `_label_pattern` used to compile every label into a `filters.Regex` at
    handler-build time, so `reload_translations()` — or an admin retitling a
    label in the admin UI — changed what the KEYBOARD renders while the MATCHER
    kept hunting for the old string. The button the customer could see was dead
    until a restart, and their tap fell through to the group-0 catch-all, where
    a Cancel became a support ticket with no reply.

    `MenuTapFilter` asks `_resolve_tapped_label` WHEN THE TAP ARRIVES, off the
    same `i18n.get` the keyboard builder renders from, so the two can no longer
    describe different strings. The retired copy stops matching in the same
    instant, which is the other half of the guarantee: it cannot linger as an
    invisible hotword that hijacks typed text.
    """
    table = dict(TRANSLATIONS)
    bot = await build_bot_harness(
        monkeypatch, translations=table, database=customer_speaking(UZ)
    )
    user = bot.updates()

    # An admin retitles the button at runtime. The table object is the live one
    # `i18n.get` reads, exactly as a reload would be.
    reseeded = "✍️ Manzilni o'zim yozaman"
    retired = copy_in("telegram.address.enter_manually_button", UZ)
    assert retired != reseeded
    table[(UZ, "telegram.address.enter_manually_button")] = reseeded

    assert await open_address_flow(bot, user) == ADDRESS_LOCATION
    assert reseeded in bot.telegram.last_shown().button_labels(), (
        "the rendered keyboard picks the new copy up immediately"
    )

    # The pre-reseed copy is no longer on anyone's screen, and no longer routes.
    await bot.send(user.text(retired))
    assert bot.conversation_state("address_conversation") == ADDRESS_LOCATION, (
        "the retired label still steers the flow — it is a hotword no button "
        "renders, so ordinary typed text can trip it"
    )

    await bot.send(user.text(reseeded))
    assert bot.conversation_state("address_conversation") == ADDRESS_REGION, (
        "the button the customer can see did nothing: the matcher is still "
        "frozen at handler-build time"
    )


# ===========================================================================
# Keys that were never seeded
# ===========================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    [
        "telegram.cancel",
        "telegram.address.enter_manually_button",
        "telegram.address.reenter_manually_button",
        "telegram.address.share_location_button",
    ],
)
@pytest.mark.parametrize("language", SUPPORTED)
def test_an_unseeded_reply_keyboard_label_is_never_empty(key, language):
    """An empty button label is not a cosmetic problem.

    Telegram rejects a whole `sendMessage` whose keyboard carries a
    zero-length button, so one unseeded label takes the entire screen with it —
    the customer gets nothing at all, not a button with no text. Every one of
    these keys renders a reply-keyboard button.
    """
    translation = Translation()
    translation.translations = {}

    value = translation.get(key, language)

    assert value.strip(), f"{key!r} rendered empty in {language!r}"
    assert value == Translation.humanised_missing_key(key), (
        "the documented fallback is the humanised key tail; anything else means "
        "get() grew a second, undocumented missing-key path"
    )
    assert "{" not in value and "}" not in value
    assert value != key, "the raw dotted key must never reach a customer"


async def test_a_bot_with_no_translations_at_all_still_renders_a_tappable_menu(monkeypatch):
    """The first minute after a deploy to a database whose seed never ran.

    Every key falls through to `humanised_missing_key`. That is ugly, and it is
    supposed to be survivable: a menu of humanised keys is recoverable, a
    rejected keyboard is a bot that does nothing at all.
    """
    bot = await build_bot_harness(monkeypatch, database=customer_speaking(UZ))
    user = bot.updates()

    await bot.send(user.tap("back_to_main"))

    menu = bot.telegram.last_shown()
    assert menu.text == Translation.humanised_missing_key("telegram.main_menu") == "Main menu"
    labels = menu.button_labels()
    assert labels, "the main menu rendered with no buttons at all"
    assert all(label.strip() for label in labels), (
        f"an empty button label makes Telegram refuse the whole keyboard: {labels}"
    )
    assert Translation.humanised_missing_key("telegram.menu.products") in labels
    assert "Products" in labels and "Cart title" in labels


async def test_an_unseeded_reply_label_is_still_matched_by_its_own_handler(monkeypatch):
    """The humanised fallback has to be self-consistent.

    The keyboard and the regex derive the label from the same `i18n.get` call,
    so an unseeded bot renders "Enter manually button" AND matches it. If those
    two ever derived the fallback differently, an unseeded deployment would show
    buttons that cannot be pressed — the worst possible failure to diagnose,
    because the screen looks fine.
    """
    bot = await build_bot_harness(monkeypatch, database=customer_speaking(UZ))
    user = bot.updates()
    assert await open_address_flow(bot, user) == ADDRESS_LOCATION

    fallback_label = Translation.humanised_missing_key(
        "telegram.address.enter_manually_button"
    )
    assert fallback_label == "Enter manually button"
    assert fallback_label in bot.telegram.last_shown().button_labels()

    await bot.send(user.text(fallback_label))

    assert bot.conversation_state("address_conversation") == ADDRESS_REGION


# ===========================================================================
# Placeholders in seeded copy
# ===========================================================================


async def test_a_placeholder_the_call_site_never_fills_is_never_shown_to_the_customer(monkeypatch):
    """Incident: copy seeded with a placeholder nobody fills reached the customer.

    `Translation.get` only calls `.format()` when the CALL SITE passes
    args/kwargs. `main_menu_handler` passes none, so copy seeded as
    "Salom, {first_name}!" used to be delivered with the braces intact.
    Translation values are editable from the admin UI, so this is one careless
    paste away at any time — which is why the guard lives in `get` (every
    render passes through it) rather than in the seeder (which the admin UI
    bypasses).

    GUARANTEE: `get` never emits an unresolved `{...}`. A template it cannot
    resolve is broken copy and degrades to the key's humanised form, which
    reads as a plain label instead of as a bug.

    Uses the REAL `Translation.get`: the harness's stand-in is deliberately
    more forgiving, and this test is about production's own rule.
    """
    bot = await build_bot_harness(
        monkeypatch, translations=TRANSLATIONS, database=customer_speaking(UZ)
    )
    monkeypatch.setattr(
        i18n_singleton,
        "get",
        production_i18n_serving(
            UZ, **{"telegram.main_menu": "💧 Salom, {first_name}! Nima qilamiz?"}
        ).get,
    )
    user = bot.updates()

    await bot.send(user.tap("back_to_main"))

    menu = bot.telegram.last_shown()
    assert "{" not in menu.text, (
        f"an unfilled placeholder reached the customer verbatim: {menu.text!r}"
    )
    assert menu.text == Translation.humanised_missing_key("telegram.main_menu")
    labels = menu.button_labels()
    assert labels and all(label.strip() for label in labels), (
        f"broken copy must not cost the customer the menu itself: {labels}"
    )


async def test_a_renamed_placeholder_costs_the_address_but_not_the_flow(monkeypatch):
    """A translator renames `{address}` and the customer stops being able to
    check where the bot thinks they live — but keeps the flow.

    `get` catches the `KeyError`, so the pin flow survives, which is the
    important half. The geocoded street is unavoidably lost: the copy no longer
    has a slot the call site can fill.

    GUARANTEE: what replaces it is the humanised key, NOT the raw template. The
    customer is never shown `{manzil}`, and never confirms a screen whose only
    content is a translator's typo. Uses the REAL `Translation.get` — the
    harness's stand-in keeps the raw template on purpose.
    """
    bot = await build_bot_harness(
        monkeypatch, translations=TRANSLATIONS, database=customer_speaking(UZ)
    )
    monkeypatch.setattr(
        i18n_singleton,
        "get",
        production_i18n_serving(
            UZ,
            **{
                "telegram.address.detected_location_prefix": (
                    "📍 *Aniqlangan joylashuv:*\n{manzil}\n\n"
                )
            },
        ).get,
    )
    user = bot.updates()
    await open_address_flow(bot, user)

    bot.telegram.reset()
    await bot.send(user.location(PIN_LAT, PIN_LNG))

    assert bot.conversation_state("address_conversation") == ADDRESS_TITLE, (
        "a bad placeholder must not kill the flow"
    )
    prompt = bot.telegram.last_shown()
    assert "{manzil}" not in prompt.text, "the raw template must never be shown"
    assert Translation.humanised_missing_key(
        "telegram.address.detected_location_prefix"
    ) in prompt.text
    # The prompt that carries the flow forward is still there, in Uzbek.
    assert copy_in("telegram.address.title_prompt", UZ) in prompt.text


def production_i18n_serving(language: str, **overrides) -> Translation:
    """A REAL :class:`Translation` loaded with this file's copy.

    The harness's stand-in for ``i18n.get`` is deliberately more forgiving than
    production — among other things it catches ``IndexError``, which is exactly
    the difference the two tests below are about. Anything that needs
    production's own interpolation behaviour swaps this in after the harness is
    built; the singleton is the same object every bot module imported, so one
    setattr reaches all of them.
    """
    translation = Translation()
    translation.translations = {
        language: {
            key: value
            for (seeded_language, key), value in TRANSLATIONS.items()
            if seeded_language == language
        }
    }
    translation.translations[language].update(overrides)
    return translation


async def test_correct_placeholder_copy_puts_the_detected_street_on_the_screen(monkeypatch):
    """The control for the placeholder tests either side of it.

    Same real ``Translation.get``, same pin flow, copy whose placeholder matches
    the call site's kwargs — and the customer reaches the title step with the
    reverse-geocoded street in front of them. Without this, both ratchets would
    keep passing if the pin flow broke for some entirely unrelated reason.
    """
    bot = await build_bot_harness(
        monkeypatch, translations=TRANSLATIONS, database=customer_speaking(UZ)
    )
    monkeypatch.setattr(i18n_singleton, "get", production_i18n_serving(UZ).get)

    user = bot.updates()
    await open_address_flow(bot, user)

    bot.telegram.reset()
    await bot.send(user.location(PIN_LAT, PIN_LNG))

    assert bot.conversation_state("address_conversation") == ADDRESS_TITLE
    prompt = bot.telegram.last_shown()
    assert GEOCODED in prompt.text, (
        "the geocoded street really is interpolated into this message — which "
        "is what makes a broken placeholder a live hazard"
    )
    assert "addr_title_home" in prompt.callback_data()


@pytest.mark.unit
def test_positional_placeholder_copy_is_caught_instead_of_escaping_i18n_get():
    """Incident: an `IndexError` escaped `Translation.get` into its caller.

    The interpolation guard was `except (KeyError, ValueError)`. A value
    carrying a POSITIONAL placeholder — `{}` or `{0}` — interpolated with
    keyword arguments raises `IndexError`, which that clause did not catch, so
    the exception escaped `i18n.get` and took the calling flow down.

    `{}` is not exotic: translation values are free-text fields in the admin UI,
    and normalising `{address}` to `{}` is the kind of thing a translator does.

    GUARANTEE: every un-renderable shape is caught by the one rendering rule
    (`shared.i18n_rendering.render_translation`, which `staff_bot/i18n.py` now
    shares) and degrades to the humanised key. `get` does not raise, and does
    not hand the customer a raw template either.
    """
    translation = Translation()
    translation.translations = {
        UZ: {"telegram.address.detected_location_prefix": "📍 *Joylashuv:*\n{}\n\n"}
    }

    assert translation.get(
        "telegram.address.detected_location_prefix", UZ, address=GEOCODED
    ) == Translation.humanised_missing_key("telegram.address.detected_location_prefix")

    # The neighbouring shapes are caught by the same clause — the point being
    # that ALL of them now take the same, brace-free exit.
    translation.translations[UZ]["telegram.a"] = "{nomalum}"
    translation.translations[UZ]["telegram.b"] = "{"
    assert translation.get("telegram.a", UZ, address=GEOCODED) == "A"
    assert translation.get("telegram.b", UZ, address=GEOCODED) == "B"


@pytest.mark.unit
def test_the_two_bots_render_a_broken_translation_row_identically():
    """One bad row cannot mean two different things on the two bots.

    Both `Translation` classes read different rows and track missing keys
    differently, but the rule that turns a VALUE into what a human reads is one
    expression in `shared.i18n_rendering`. This is the test that fails if either
    bot grows a second copy of it.
    """
    from staff_bot.i18n import Translation as StaffTranslation

    customer, staff = Translation(), StaffTranslation()
    broken = {
        "positional": "{}",
        "renamed": "{nomalum}",
        "malformed": "{",
        "never_filled": "Salom, {first_name}!",
        "fine": "Salom!",
    }
    customer.translations = {UZ: {f"telegram.{k}": v for k, v in broken.items()}}
    staff.translations = {UZ: {f"staff.{k}": v for k, v in broken.items()}}

    for name in broken:
        assert customer.get(f"telegram.{name}", UZ, address=GEOCODED) == staff.get(
            f"staff.{name}", UZ, address=GEOCODED
        ), f"the two bots disagree about the '{name}' row when kwargs are passed"
        assert customer.get(f"telegram.{name}", UZ) == staff.get(f"staff.{name}", UZ), (
            f"the two bots disagree about the '{name}' row with no kwargs"
        )


async def test_positional_placeholder_copy_no_longer_kills_the_pin_flow(monkeypatch):
    """Incident: what the escaping `IndexError` above did to a customer.

    `location_received` wraps its whole body in `except Exception: return
    ConversationHandler.END`. So one translation value with `{}` in it meant:
    the customer shares their pin, reads "Joylashuv qabul qilindi!", and then
    nothing — no prompt, no error, no address. Deterministically, for every
    customer in that language, until someone edited the row back.

    Uses the REAL `Translation.get` rather than the harness's stand-in, because
    the harness catches `IndexError` of its own accord and the whole point of
    this test is what production does.

    GUARANTEE: broken copy costs the customer that one sentence and nothing
    else — the title prompt still arrives and the flow reaches ADDRESS_TITLE.
    """
    bot = await build_bot_harness(
        monkeypatch, translations=TRANSLATIONS, database=customer_speaking(UZ)
    )

    production_i18n = production_i18n_serving(
        UZ,
        **{"telegram.address.detected_location_prefix": "📍 *Aniqlangan joylashuv:*\n{}\n\n"},
    )
    monkeypatch.setattr(i18n_singleton, "get", production_i18n.get)

    user = bot.updates()
    await open_address_flow(bot, user)

    bot.telegram.reset()
    await bot.send(user.location(PIN_LAT, PIN_LNG))

    assert bot.conversation_state("address_conversation") == ADDRESS_TITLE, (
        "the flow must survive one broken translation row"
    )
    texts = bot.telegram.texts()
    assert texts[0] == copy_in("telegram.address.location_received", UZ)
    assert copy_in("telegram.address.title_prompt", UZ) in texts[-1], (
        f"the customer never reached the prompt that carries the flow: {texts}"
    )
    assert "{}" not in texts[-1]
