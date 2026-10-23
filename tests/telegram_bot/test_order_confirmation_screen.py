"""The order confirmation screen, through the real dispatcher (owner 2026-10-23).

The customer must see at once which step this is and what they will pay:
* the title and the Delivery address / Payment method labels are bold, and so is the "To pay" line;
* the money reads top to bottom as one sum: the lines, the tier discount, then the prepaid balance
  as a deduction, and "To pay" is what is left to pay at the door;
* one line says what prepaid balance is left after this order, and only when some is;
* Apply reward and Edit share a row; there is no Change address button (Back, Back reaches the
  address step).

The screen is sent as HTML, so every address, product and reward name is escaped. The transport
is strict: a message Telegram would refuse is refused here too. Copy is the real seeded copy.
"""

from __future__ import annotations

import json

import pytest

import bot as bot_module  # noqa: F401

# Bare bot-module names: already in sys.modules after `import bot`.
import utils as utils_module
from keyboards import OrderKeyboards
from menu_keyboard import MENU_KEYBOARD_STATE_KEY, wanted_menu_labels

# AFTER the bot import, for the sys.path reason test_agent_order_callbacks.py documents.
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS  # noqa: E402

from tests.telegram_bot.ptb_harness import build_bot_harness
from tests.telegram_bot.test_checkout_journey_dispatcher import (
    BOTTLE_19L,
    Shop,
    add_address,
    reach_confirmation,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

TRANSLATIONS = {
    (language, key): row[language]
    for key, row in BACKEND_TRANSLATIONS.items()
    if key.startswith("telegram.")
    for language in ("uz", "ru", "en")
    if row.get(language)
}

HOME = 901
GOLD = {
    "tier_discount": 3600.0,
    "tier_name": "Gold",
    "tier_discount_percentage": 3.0,
    "total_discount": 3600.0,
    "final_total": 116400.0,
}


def uz(key: str, **fmt) -> str:
    return BACKEND_TRANSLATIONS[key]["uz"].format(**fmt)


async def _always_allowed(*_args, **_kwargs):
    return True


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS, strict_entities=True)
    monkeypatch.setattr(utils_module.rate_limiter, "allow_request", _always_allowed)
    harness.database.user["bot_state"] = json.dumps({
        MENU_KEYBOARD_STATE_KEY: {
            "labels": wanted_menu_labels("uz", True), "language": "uz", "loyalty": True,
        },
    })
    return harness


@pytest.fixture
def shop(bot, monkeypatch):
    shop = Shop(bot.backend, monkeypatch)
    shop.cart[BOTTLE_19L] = 8  # 8 × 15 000 = 120 000
    shop.estimate_pricing.update(GOLD)
    return shop


@pytest.fixture
def user(bot):
    return bot.updates()


def rows(screen) -> list[list[str]]:
    return [[button["callback_data"] for button in row] for row in screen.reply_markup["inline_keyboard"]]


async def test_a_cash_order_covered_by_the_prepaid_balance_reads_as_one_sum(bot, shop, user):
    add_address(bot, HOME, "Uy", "Chilonzor 9-kvartal, 15-uy")
    shop.estimate_cod_prepayment = {
        "available_balance": 213500.0, "applied": 116400.0, "payable_after": 0.0, "balance_after": 97100.0,
    }

    await reach_confirmation(bot, user, HOME, "cash")

    screen = bot.telegram.last_shown()
    assert screen.params.get("parse_mode") == "HTML"
    assert screen.refused is None
    assert screen.text == (
        "<b>Buyurtmani tasdiqlash</b>\n"
        "\n"
        "<b>Yetkazib berish manzili:</b> Uy\n"
        "Chilonzor 9-kvartal, 15-uy\n"
        "\n"
        "<b>To'lov usuli:</b> 💰 Naqd pul\n"
        "\n"
        "• Aqua Element 19L × 8 — 120,000 UZS\n"
        "Gold chegirma −3% 🏷 — −3,600 UZS\n"
        "Oldindan to'langan qoldiq — −116,400 UZS\n"
        "────────────────\n"
        "<b>💰 To'lash: 0 UZS</b>\n"
        "\n"
        "💳 Ushbu buyurtmadan so'ng oldindan to'langan qoldiq: 97,100 UZS"
    )


async def test_a_balance_that_covers_part_leaves_no_balance_after_line(bot, shop, user):
    add_address(bot, HOME, "Uy", "Chilonzor 9-kvartal, 15-uy")
    shop.estimate_cod_prepayment = {
        "available_balance": 50000.0, "applied": 50000.0, "payable_after": 66400.0, "balance_after": 0.0,
    }

    await reach_confirmation(bot, user, HOME, "cash")

    assert bot.telegram.last_shown().text.endswith(
        "Gold chegirma −3% 🏷 — −3,600 UZS\n"
        "Oldindan to'langan qoldiq — −50,000 UZS\n"
        "────────────────\n"
        "<b>💰 To'lash: 66,400 UZS</b>"
    )


async def test_a_cash_order_with_no_prepaid_balance_has_no_prepaid_lines(bot, shop, user):
    add_address(bot, HOME, "Uy", "Chilonzor 9-kvartal, 15-uy")
    shop.estimate_cod_prepayment = {
        "available_balance": 0.0, "applied": 0.0, "payable_after": 116400.0, "balance_after": 0.0,
    }

    await reach_confirmation(bot, user, HOME, "cash")

    text = bot.telegram.last_shown().text
    assert text.endswith("────────────────\n<b>💰 To'lash: 116,400 UZS</b>")
    assert "Oldindan to'langan qoldiq" not in text


async def test_a_card_order_never_shows_the_prepaid_balance(bot, shop, user):
    """The balance is spent on cash orders only; the server's card quote carries no deduction."""
    add_address(bot, HOME, "Uy", "Chilonzor 9-kvartal, 15-uy")
    shop.estimate_cod_prepayment = {
        "available_balance": 213500.0, "applied": 116400.0, "payable_after": 0.0, "balance_after": 97100.0,
    }

    await reach_confirmation(bot, user, HOME, "card")

    text = bot.telegram.last_shown().text
    assert "<b>To'lov usuli:</b> 💳 Karta\n" in text
    assert text.endswith("────────────────\n<b>💳 To'lash: 116,400 UZS</b>")
    assert "qoldiq" not in text


async def test_apply_reward_and_edit_share_a_row_and_there_is_no_change_address(bot, shop, user):
    add_address(bot, HOME, "Uy", "Chilonzor 9-kvartal, 15-uy")

    await reach_confirmation(bot, user, HOME, "cash")

    screen = bot.telegram.last_shown()
    assert rows(screen) == [
        ["confirm_order", "cancel_order"],
        ["checkout_choose_reward", "edit_order"],
        [f"back_to_payment_{HOME}"],
    ]
    assert screen.button_labels() == [
        uz("telegram.order.confirm"), uz("telegram.cancel"),
        "🎁 " + uz("telegram.loyalty.apply_reward"), uz("telegram.order.edit"),
        uz("telegram.back"),
    ]


def test_a_chosen_reward_keeps_its_change_and_remove_row_and_edit_gets_its_own():
    markup = OrderKeyboards.order_confirmation(
        "uz", meets_minimum=True, has_reward=True, show_reward=True, address_id=HOME,
    )

    assert [[b.callback_data for b in row] for row in markup.inline_keyboard] == [
        ["confirm_order", "cancel_order"],
        ["checkout_choose_reward", "checkout_remove_reward"],
        ["edit_order"],
        [f"back_to_payment_{HOME}"],
    ]


async def test_names_that_look_like_html_are_shown_as_typed(bot, shop, user):
    """An address or product name with "<" or "&" would make Telegram refuse the whole screen."""
    add_address(bot, HOME, "Uy <2>", "Chilonzor & Co, 9-kvartal")
    shop.products[BOTTLE_19L]["name"] = "Suv <19L> & co"

    await reach_confirmation(bot, user, HOME, "cash")

    screen = bot.telegram.last_shown()
    assert screen.refused is None, screen.refused
    assert "<b>Yetkazib berish manzili:</b> Uy &lt;2&gt;\nChilonzor &amp; Co, 9-kvartal\n" in screen.text
    assert "• Suv &lt;19L&gt; &amp; co × 8 — 120,000 UZS\n" in screen.text
