"""The persistent bottom keyboard as a customer sees it (spec 2026-10-21 §1).

Shared by every test file that drives the keyboard. It imports production modules and the seed,
never another test module, so any test file can import it without an import cycle.
"""

import json

from menu_keyboard import MENU_KEYBOARD_STATE_KEY, wanted_menu_labels
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

# Every customer-bot key in the real seed, keyed the way ptb_harness._install_translations reads it.
SEEDED = {
    (language, key): value
    for key, values in BACKEND_TRANSLATIONS.items()
    if key.startswith("telegram.") and isinstance(values, dict)
    for language, value in values.items()
}


def seeded(key, language="uz"):
    return BACKEND_TRANSLATIONS[key][language]


# The seeded labels, pinned verbatim. The keyboard renders each value as-is, emoji included.
UZ_LABELS = {
    "telegram.menu.products": "💧 Suv buyurtma berish",
    "telegram.menu.orders": "📦 Buyurtmalarim",
    "telegram.cart_title": "🛒 Savatim",
    "telegram.menu.subscriptions": "🔄 Avto-yetkazib berish",
    "telegram.menu.loyalty": "🎁 Aqua Club",
    "telegram.menu.profile": "👤 Profilim",
    "telegram.menu.support": "🆘 Yordam",
}
RU_LABELS = {
    "telegram.menu.products": "💧 Заказать воду",
    "telegram.menu.orders": "📦 Мои заказы",
    "telegram.cart_title": "🛒 Моя корзина",
    "telegram.menu.subscriptions": "🔄 Автодоставка",
    "telegram.menu.loyalty": "🎁 Aqua Club",
    "telegram.menu.profile": "👤 Мой профиль",
    "telegram.menu.support": "🆘 Помощь",
}
UZ_ROWS = [
    [{"text": "💧 Suv buyurtma berish"}],
    [{"text": "📦 Buyurtmalarim"}, {"text": "🛒 Savatim"}],
    [{"text": "🔄 Avto-yetkazib berish"}, {"text": "🎁 Aqua Club"}],
    [{"text": "👤 Profilim"}, {"text": "🆘 Yordam"}],
]
UZ_ROWS_WITHOUT_AQUA_CLUB = [UZ_ROWS[0], UZ_ROWS[1], [{"text": "🔄 Avto-yetkazib berish"}], UZ_ROWS[3]]


def expected_labels(language="uz", loyalty=True):
    """The labels production renders from whatever table the harness installed, in layout order."""
    return list(wanted_menu_labels(language, show_loyalty=loyalty).values())


def menu_sends(bot):
    """Every sendMessage that carried the BOTTOM keyboard: a reply keyboard with no contact or location button."""
    return [
        call for call in bot.telegram.of("sendMessage")
        if "keyboard" in call.reply_markup
        and not any(
            button.get("request_contact") or button.get("request_location")
            for row in call.reply_markup["keyboard"] for button in row
        )
    ]


def stored_marker(bot):
    """The install marker in the harness's users row. RegistrationDatabase keeps its row in `.row`."""
    row = getattr(bot.database, "row", bot.database.user)
    raw = (row or {}).get("bot_state") or "{}"
    return json.loads(raw).get(MENU_KEYBOARD_STATE_KEY)


def assert_no_edit_carries_a_reply_keyboard(bot):
    """An edited message can only carry inline buttons (spec §1)."""
    for call in bot.telegram.of("editMessageText", "editMessageReplyMarkup", "editMessageCaption"):
        assert "keyboard" not in call.reply_markup, f"{call.method} carried a reply keyboard: {call.reply_markup}"
