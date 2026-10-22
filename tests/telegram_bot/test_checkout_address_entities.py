"""The checkout "Delivering to" card and order details render a saved address
as DATA, through one escaped block (`address_text.address_block_markdown_v2`).

Incident 2026-10-18: `_show_address_confirmation` sent the title and the full
address raw in legacy Markdown. A geocoded o' spelled as ONE backtick opened a
code entity that never closed, Telegram refused the card 4 times, and the cart
path deleted the cart before its resend failed the same way. Every test here
runs on `strict_entities=True`: the transport refuses what Telegram refuses.
"""

from __future__ import annotations

import pytest

from tests.telegram_bot.ptb_harness import build_bot_harness
from tests.telegram_bot.test_checkout_journey_dispatcher import (  # noqa: F401  (shop, user are fixtures)
    BOTTLE_19L,
    TRANSLATIONS,
    Shop,
    add_address,
    assert_no_swallowed_crash,
    fill_cart,
    shop,
    user,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PIN_LAT, PIN_LNG = 41.32354, 69.241036
BACKTICK_ADDRESS = "15, Navoiy ko`chasi, Toshkent"

# The real seeded copy (dev DB, 2026-10-21); none of it carries a MarkdownV2 reserved character.
DELIVERING_TO = {
    "uz": "📍 Yetkazib berish manzili:",
    "ru": "📍 Доставка по адресу:",
    "en": "📍 Delivering to:",
}
STRICT_TRANSLATIONS = {
    **TRANSLATIONS,
    **{(lang, "telegram.checkout.delivering_to"): label for lang, label in DELIVERING_TO.items()},
    # The pin flow's title prompt in its real seeded shape; its own *bold* is why it is legacy Markdown.
    "telegram.address.detected_location_prefix": "📍 *Aniqlangan joylashuv:*\n{address}\n\n",
    "telegram.address.title_prompt": "Manzilga nom bering:",
    # An owner-edited label carrying MarkdownV2-reserved characters: labels are escaped too.
    ("uz", "telegram.orders.delivery_info"): "Yetkazib berish (manzil)",
}
UZ_CARD = "📍 Yetkazib berish manzili:\n*Uy*\n15, Navoiy ko\\`chasi, Toshkent"
HOSTILE = ["`", "_", "*", "[", ".", "-", "(", "!"]


@pytest.fixture
async def bot(monkeypatch):
    """The checkout journey's bot behind a transport that parses entities like Telegram."""
    return await build_bot_harness(monkeypatch, translations=STRICT_TRANSLATIONS, strict_entities=True)


def refusals(bot) -> list[str]:
    """Every 400 Telegram answered, in order. Empty means the customer was refused nothing."""
    return [call.refused for call in bot.telegram.calls if call.refused]


async def add_first_address_from_checkout(bot, user, geocoded: str) -> int:
    """Crash #1's path: zero-address Checkout -> pin -> "Uy" -> three Skips. After that,
    `_resume_checkout_after_address` draws the card on the MESSAGE branch (callback_query=None)."""
    bot.backend.route(
        "POST", "/api/v1/addresses/reverse-geocode",
        lambda _call: {"data": {"formatted_address": geocoded}},
    )
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await bot.send(user.tap("cart_view"))
    await bot.send(user.tap("cart_checkout"))
    await bot.send(user.location(PIN_LAT, PIN_LNG))
    await bot.send(user.tap("addr_title_home"))
    for skip in ("skip_apartment", "skip_floor", "skip_delivery_instructions"):
        await bot.send(user.tap(skip))
    (address_id,) = bot.backend.addresses
    return address_id


async def test_the_card_after_a_new_pin_shows_a_backtick_address_on_the_message_path(bot, shop, user):
    address_id = await add_first_address_from_checkout(bot, user, BACKTICK_ADDRESS)

    assert refusals(bot) == [], "Telegram refused a message on the way to the card"
    card = bot.telegram.delivered[-1]
    assert card.method == "sendMessage"
    assert card.params["parse_mode"] == "MarkdownV2"
    assert card.text == UZ_CARD
    assert card.callback_data() == [f"address_{address_id}", "add_new_address_checkout", "back_to_cart"]
    assert "Error occurred" not in bot.telegram.texts()


async def test_cart_checkout_edits_the_card_in_place_and_never_deletes_the_cart(bot, shop, user):
    add_address(bot, 900, "Uy", BACKTICK_ADDRESS)
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await bot.send(user.tap("cart_view"))
    bot.telegram.reset()

    await bot.send(user.tap("cart_checkout"))

    assert refusals(bot) == []
    (card,) = bot.telegram.of("editMessageText")
    assert card.params["parse_mode"] == "MarkdownV2"
    assert card.text == UZ_CARD
    assert card.callback_data() == ["address_900", "add_new_address_checkout", "back_to_cart"]
    assert bot.telegram.of("deleteMessage") == [], "the cart vanished on 2026-10-18 exactly here"
    assert_no_swallowed_crash(bot)


@pytest.mark.parametrize("language", ["uz", "ru", "en"])
@pytest.mark.parametrize("field", ["label", "title", "full_address"])
@pytest.mark.parametrize("char", HOSTILE)
async def test_any_markdown_character_in_the_label_title_or_address_still_draws_the_card(
    monkeypatch, language, field, char
):
    label = DELIVERING_TO[language]
    translations = dict(STRICT_TRANSLATIONS)
    title, full_address = "Uy", "Chilonzor 15"
    if field == "label":
        translations[(language, "telegram.checkout.delivering_to")] = f"{label} {char}"
    elif field == "title":
        title = f"Uy {char} 1"
    else:
        full_address = f"Chilonzor {char} 15"
    harness = await build_bot_harness(monkeypatch, translations=translations, strict_entities=True)
    harness.database.user["preferred_language"] = language
    Shop(harness.backend, monkeypatch)
    user = harness.updates()
    add_address(harness, 900, title, full_address)
    await fill_cart(harness, user, BOTTLE_19L, quantity=3)
    await harness.send(user.tap("cart_view"))

    await harness.send(user.tap("cart_checkout"))

    assert refusals(harness) == []
    card = harness.telegram.delivered[-1]
    assert card.params["parse_mode"] == "MarkdownV2"
    assert card.text == {
        "label": f"{label} \\{char}\n*Uy*\nChilonzor 15",
        "title": f"{label}\n*Uy \\{char} 1*\nChilonzor 15",
        "full_address": f"{label}\n*Uy*\nChilonzor \\{char} 15",
    }[field]
    assert card.callback_data() == ["address_900", "add_new_address_checkout", "back_to_cart"]
    assert_no_swallowed_crash(harness)


def _order_with_address(title, full_address):
    """GET /api/v1/orders/<id> for a pending cash order (shape: test_case_b_pay_button_dispatcher._order)."""
    return {
        "id": 1125,
        "order_number": "TG_001125_26",
        "status": "pending",
        "is_paid": False,
        "payment_method": "cash",
        "total_amount": 18000,
        "created_at": "2026-10-18T11:54:00+00:00",
        "order_items": [],
        "can_customer_cancel": True,
        "payment_info": {
            "id": 1229, "payment_method": "cash", "payment_status": "pending",
            "payment_provider": "cash", "amount": 18000, "amount_collected": 0,
            "outstanding_amount": 18000, "payment_link": None,
            "is_payable": False, "payable_payment_link": None,
        },
        "delivery_address": {"id": 900, "title": title, "full_address": full_address},
    }


async def test_order_details_renders_its_delivery_address_through_the_same_block(bot, user):
    bot.backend.route(
        "GET", "/api/v1/orders/1125",
        lambda _call: {"data": {
            "order": _order_with_address("Uy *1*", "12-uy, Navoiy ko`chasi (A)."), "delivery": None,
        }},
    )

    await bot.send(user.tap("order_1125"))

    assert refusals(bot) == []
    (screen,) = bot.telegram.of("editMessageText")
    assert screen.params["parse_mode"] == "MarkdownV2"
    assert screen.text.endswith(
        "\nYetkazib berish \\(manzil\\):\n*Uy \\*1\\**\n12\\-uy, Navoiy ko\\`chasi \\(A\\)\\."
    )


async def test_a_card_telegram_still_refuses_on_the_message_path_is_resent_as_plain_text(bot, shop, user):
    """The backstop for the next refusal no escaping foresaw, on the branch crash #1 took."""
    def refuse_markdown_v2(params):
        if params.get("parse_mode") != "MarkdownV2":
            return 200, {"ok": True, "result": bot.telegram._result_for("sendMessage", params)}
        return 400, {
            "ok": False, "error_code": 400,
            "description": "Bad Request: can't parse entities: Character '.' is reserved "
                           "and must be escaped with the preceding '\\'",
        }
    bot.telegram.failures["sendMessage"] = refuse_markdown_v2

    address_id = await add_first_address_from_checkout(bot, user, BACKTICK_ADDRESS)

    card = bot.telegram.delivered[-1]
    assert card.method == "sendMessage"
    assert "parse_mode" not in card.params
    assert card.text == UZ_CARD, "the same words; only the formatting is lost"
    assert card.callback_data() == [f"address_{address_id}", "add_new_address_checkout", "back_to_cart"]
    assert "Error occurred" not in bot.telegram.texts()
