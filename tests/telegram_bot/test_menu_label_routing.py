# tests/telegram_bot/test_menu_label_routing.py
"""Bottom-keyboard labels typed as messages open their screens (spec §1 "Matching taps").

A label arrives as plain text. Before the router it fell through to the group-0
catch-all and was filed to the Support Inbox with no reply.
"""
import json

import pytest

import utils as utils_module
from keyboards import MAIN_MENU_KEYS

from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, build_bot_harness
from tests.telegram_bot.test_language_and_i18n_journeys import RU, SEEDED, UZ

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SUPPORT = "/api/v1/support/messages"

# The seeded copy, from the suite's one verbatim table (SEEDED mirrors
# scripts/seed_backend_translations.py). The emoji is part of the value, and the
# keyboard renders the value as-is.
MENU_LABELS = {key: SEEDED[key] for key in MAIN_MENU_KEYS}
MENU_LABEL_TRANSLATIONS = {
    (language, key): label for key, by_language in MENU_LABELS.items() for language, label in by_language.items()
}

SCREEN_COPY = {
    "telegram.orders.no_orders": "BUYURTMALAR-YOQ",
    "telegram.cart_empty": "SAVAT-BOSH",
    "telegram.subscription.title": "OBUNALAR",
    "telegram.profile_title": "PROFIL",
    "telegram.support.menu_intro": "YORDAM-EKRANI",
    "telegram.loyalty.not_available": "🎁 Sodiqlik dasturi hisobingiz uchun mavjud emas.",
    "telegram.registration_welcome": "Xush kelibsiz! Iltimos, tilni tanlang.",
    "telegram.registration.start_command_prompt": "Iltimos, /start buyrug'i bilan boshlang.",
}

# How each screen's message starts when the harness backend holds no data.
# Products and Aqua Club title themselves with their own label key.
OPENS_WITH = {
    "telegram.menu.products": MENU_LABELS["telegram.menu.products"][UZ],
    "telegram.menu.orders": "BUYURTMALAR-YOQ",
    "telegram.cart_title": "SAVAT-BOSH",
    "telegram.menu.subscriptions": "OBUNALAR",
    "telegram.menu.loyalty": MENU_LABELS["telegram.menu.loyalty"][UZ],
    "telegram.menu.profile": "PROFIL",
    "telegram.menu.support": "YORDAM-EKRANI",
}


def _always(value):
    async def _answer(*_args, **_kwargs):
        return value

    return _answer


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(
        monkeypatch, translations={**MENU_LABEL_TRANSLATIONS, **SCREEN_COPY}
    )
    # One customer sends many texts here; "allowed" is the normal production answer.
    monkeypatch.setattr(utils_module.rate_limiter, "allow_request", _always(True))
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


def support_posts(bot):
    return [c.data for c in bot.backend.calls if c.method == "POST" and c.endpoint == SUPPORT]


def calls_carrying(bot, text):
    return [
        c for c in bot.backend.calls
        if text in json.dumps([c.data, c.params], ensure_ascii=False, default=str)
    ]


def test_the_bottom_keyboard_keys_in_layout_order():
    assert MAIN_MENU_KEYS == (
        "telegram.menu.products",
        "telegram.menu.orders",
        "telegram.cart_title",
        "telegram.menu.subscriptions",
        "telegram.menu.loyalty",
        "telegram.menu.profile",
        "telegram.menu.support",
    )


@pytest.mark.parametrize("key", MAIN_MENU_KEYS)
async def test_each_uzbek_label_typed_opens_its_screen(bot, user, key):
    label = MENU_LABELS[key][UZ]

    await bot.send(user.text(label))

    assert bot.telegram.last_shown().text.startswith(OPENS_WITH[key])
    assert support_posts(bot) == [], "a label is navigation, not a support message"
    assert calls_carrying(bot, label) == []


@pytest.mark.parametrize("key", ["telegram.menu.orders", "telegram.menu.support"])
async def test_a_russian_label_opens_the_same_screen_for_an_uzbek_customer(bot, user, key):
    await bot.send(user.text(MENU_LABELS[key][RU]))

    assert bot.telegram.last_shown().text.startswith(OPENS_WITH[key])
    assert support_posts(bot) == []


async def test_the_cart_label_opens_the_plain_cart_even_mid_edit(bot, user):
    bot.application.user_data[DEFAULT_USER_ID]["cart_edit_return"] = "order_confirm"

    await bot.send(user.text(MENU_LABELS["telegram.cart_title"][UZ]))

    assert bot.telegram.last_shown().text == "SAVAT-BOSH"
    assert "cart_edit_return" not in bot.application.user_data[DEFAULT_USER_ID]


async def test_an_ineligible_customer_tapping_aqua_club_is_told_it_is_not_available(bot, user):
    bot.database.loyalty_eligible = False

    await bot.send(user.text(MENU_LABELS["telegram.menu.loyalty"][UZ]))

    assert bot.telegram.last_shown().text == SCREEN_COPY["telegram.loyalty.not_available"]
    assert [c.endpoint for c in bot.backend.calls if "loyalty" in c.endpoint] == []
    assert support_posts(bot) == []


async def test_an_unregistered_customer_tapping_a_label_loses_the_keyboard_and_is_sent_to_start(bot, user):
    bot.database.user = None

    await bot.send(user.text(MENU_LABELS["telegram.menu.orders"][UZ]))

    shown = bot.telegram.last_shown()
    assert shown.text == (
        f"{SCREEN_COPY['telegram.registration_welcome']}\n\n"
        f"{SCREEN_COPY['telegram.registration.start_command_prompt']}"
    )
    assert shown.reply_markup == {"remove_keyboard": True}
    assert support_posts(bot) == []
