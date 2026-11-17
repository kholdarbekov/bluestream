"""The coming catalogue price, dated, on the product list and the quantity screen (Task 45).

Driven through the real dispatcher with the payload the server publishes (`pricing.upcoming_price`
on `GET /api/v1/products/` and `/products/<id>`). The bot prints the server's two fields; it
decides nothing and computes nothing.
"""

from __future__ import annotations

import copy
import html

import pytest
from telegram.helpers import escape_markdown

import bot as bot_module  # noqa: F401
import utils as utils_module

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS  # noqa: E402

from tests.telegram_bot.ptb_harness import build_bot_harness
from tests.telegram_bot.test_checkout_journey_dispatcher import (
    BOTTLE_19L,
    BOTTLE_5L,
    PRODUCTS,
    WATER,
    Shop,
    add_address,
    assert_no_swallowed_crash,
    fill_cart,
    reach_confirmation,
    reach_payment_picker,
)
from tests.telegram_bot.test_quantity_screen_journeys import (
    TRANSLATIONS,
    _always_allowed,
    body,
    uz,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

UPCOMING = {"new_price": 20000.0, "effective_date": "2027-02-01"}


def _catalogue(upcoming=UPCOMING):
    products = copy.deepcopy(PRODUCTS)
    if upcoming is not None:
        products[BOTTLE_19L]["pricing"]["upcoming_price"] = dict(upcoming)
    return products


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS, strict_entities=True)
    monkeypatch.setattr(utils_module.rate_limiter, "allow_request", _always_allowed)
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


def _shop(bot, monkeypatch, upcoming=UPCOMING):
    return Shop(bot.backend, monkeypatch, products=_catalogue(upcoming))


def _line(language, price):
    return "📅 " + BACKEND_TRANSLATIONS["telegram.price_change.upcoming_line"][language].format(
        date="01.02.2027", price=price,
    )


async def test_the_product_list_states_the_coming_price_under_the_changing_product_only(bot, monkeypatch, user):
    _shop(bot, monkeypatch)

    await bot.send(user.tap("menu_products"))
    await bot.send(user.tap(f"category_{WATER}"))

    listing = bot.telegram.last_shown()
    assert listing.params.get("parse_mode") == "MarkdownV2"
    lines = listing.text.split("\n\n")
    assert len(lines) == 2
    changing, steady = (block.split("\n") for block in lines)
    assert changing[2] == "   " + escape_markdown(_line("uz", "20,000 so'm"), version=2)
    assert len(changing) == 3
    assert len(steady) == 2, "a product with no announced change keeps its two lines"
    assert_no_swallowed_crash(bot)


async def test_the_quantity_screen_states_the_coming_price_under_the_unit_price(bot, monkeypatch, user):
    _shop(bot, monkeypatch)

    await bot.send(user.tap("menu_products"))
    await bot.send(user.tap(f"category_{WATER}"))
    await bot.send(user.tap(f"product_{BOTTLE_19L}"))

    lines = body(bot.telegram.last_shown()).split("\n")
    assert lines[0] == "🛒 Aqua Element 19L"
    assert lines[1].startswith("💰 ")
    assert lines[2] == _line("uz", "20,000 so'm")
    assert lines[3] == "ℹ️ " + uz("telegram.products.min_order_quantity_label", min_qty=2)
    assert_no_swallowed_crash(bot)


async def test_no_dated_line_when_the_server_publishes_no_coming_price(bot, monkeypatch, user):
    _shop(bot, monkeypatch, upcoming=None)

    await bot.send(user.tap("menu_products"))
    await bot.send(user.tap(f"category_{WATER}"))
    listing = bot.telegram.last_shown()
    assert "📅" not in listing.text
    assert all(len(block.split("\n")) == 2 for block in listing.text.split("\n\n"))

    await bot.send(user.tap(f"product_{BOTTLE_19L}"))
    assert "📅" not in body(bot.telegram.last_shown())
    assert_no_swallowed_crash(bot)


@pytest.mark.parametrize("language, word", [("uz", "so'm"), ("en", "so'm"), ("ru", "сум")])
async def test_the_coming_price_speaks_the_customers_language_and_currency_word(
    bot, monkeypatch, user, language, word
):
    _shop(bot, monkeypatch)
    bot.database.user["preferred_language"] = language

    await bot.send(user.tap("menu_products"))
    await bot.send(user.tap(f"category_{WATER}"))
    await bot.send(user.tap(f"product_{BOTTLE_19L}"))

    assert body(bot.telegram.last_shown()).split("\n")[2] == _line(language, f"20,000 {word}")
    assert_no_swallowed_crash(bot)


# -- the kept price (Task 46) ----------------------------------------------------------------------------------

KEPT = {
    "unit_price": 20000.0, "courtesy_quantity": 2, "courtesy_unit_price": 18000.0,
    "regular_quantity": 2, "discount_amount": 4000.0, "upcoming_price": None,
}
APPLIED = {"price_courtesy": {"applied": True, "expires_on": "2027-02-14"}}
ADDRESS = 900


def kept_price_shop(shop, *, quantity=4, line_total=76000.0, extras=KEPT):
    """The server's answer for a customer inside the courtesy: 2 units at 18,000 and 2 at 20,000 = 76,000."""
    shop.line_extras[BOTTLE_19L] = dict(extras)
    shop.line_total_overrides[BOTTLE_19L] = line_total
    shop.quote_extras = copy.deepcopy(APPLIED)
    return quantity


def split_line(language="uz", *, courtesy_qty=2, courtesy_price="18,000 so'm", regular_qty=2, regular_price="20,000 so'm"):
    row = BACKEND_TRANSLATIONS["telegram.price_change.courtesy_split"][language]
    return row.format(courtesy_qty=courtesy_qty, courtesy_price=courtesy_price,
                      regular_qty=regular_qty, regular_price=regular_price)


def only_line(language="uz", *, courtesy_qty=4, courtesy_price="18,000 so'm"):
    return BACKEND_TRANSLATIONS["telegram.price_change.courtesy_only"][language].format(
        courtesy_qty=courtesy_qty, courtesy_price=courtesy_price)


THANKS = BACKEND_TRANSLATIONS["telegram.price_change.courtesy_thanks"]["uz"]


async def test_the_cart_shows_the_kept_price_split_under_its_line_and_one_thank_you(bot, monkeypatch, user):
    shop = _shop(bot, monkeypatch, upcoming=None)
    quantity = kept_price_shop(shop)

    await fill_cart(bot, user, BOTTLE_19L, quantity=quantity)
    await bot.send(user.tap("cart_view"))

    lines = bot.telegram.last_shown().text.split("\n")
    at = next(i for i, line in enumerate(lines) if line.startswith("🛒 Aqua Element 19L"))
    assert "76,000" in lines[at]
    assert lines[at + 1] == "   " + split_line()
    assert bot.telegram.last_shown().text.count(THANKS) == 1
    assert lines.index(THANKS) > lines.index(lines[at + 1]), "the thank-you comes after the lines"
    assert_no_swallowed_crash(bot)


async def test_the_confirm_screen_shows_the_kept_price_split_and_ends_with_one_thank_you(bot, monkeypatch, user):
    shop = _shop(bot, monkeypatch, upcoming=None)
    quantity = kept_price_shop(shop)
    add_address(bot, ADDRESS, "Uy", "Chilonzor 15")

    await fill_cart(bot, user, BOTTLE_19L, quantity=quantity)
    await reach_confirmation(bot, user, ADDRESS, payment="cash")

    screen = bot.telegram.last_shown()
    assert screen.refused is None
    text = screen.text
    assert "\n   " + html.escape(split_line(), quote=False) + "\n" in text
    assert text.count(THANKS) == 1
    assert text.endswith(THANKS)
    assert "76,000" in text.split("────────────────")[1]
    assert_no_swallowed_crash(bot)


async def test_the_payment_picker_shows_the_split_but_saves_the_thank_you_for_confirm(bot, monkeypatch, user):
    shop = _shop(bot, monkeypatch, upcoming=None)
    quantity = kept_price_shop(shop)
    add_address(bot, ADDRESS, "Uy", "Chilonzor 15")

    await fill_cart(bot, user, BOTTLE_19L, quantity=quantity)
    await reach_payment_picker(bot, user, ADDRESS)

    text = bot.telegram.last_shown().text
    assert "\n   " + split_line() + "\n" in text
    assert THANKS not in text
    assert_no_swallowed_crash(bot)


async def test_units_within_the_cap_show_only_the_kept_price_part(bot, monkeypatch, user):
    shop = _shop(bot, monkeypatch, upcoming=None)
    extras = {**KEPT, "courtesy_quantity": 4, "regular_quantity": 0}
    quantity = kept_price_shop(shop, line_total=72000.0, extras=extras)
    add_address(bot, ADDRESS, "Uy", "Chilonzor 15")

    await fill_cart(bot, user, BOTTLE_19L, quantity=quantity)
    await bot.send(user.tap("cart_view"))
    cart = bot.telegram.last_shown().text
    assert "   " + only_line() in cart.split("\n")
    assert " + " not in cart

    await reach_confirmation(bot, user, ADDRESS, payment="cash")
    confirm = bot.telegram.last_shown().text
    assert "   " + only_line() in confirm.split("\n")
    assert " + " not in confirm
    assert_no_swallowed_crash(bot)


async def test_a_customer_outside_the_courtesy_sees_the_new_price_and_nothing_else(bot, monkeypatch, user):
    shop = _shop(bot, monkeypatch, upcoming=None)
    shop.line_extras[BOTTLE_19L] = {"unit_price": 20000.0, "courtesy_quantity": None, "regular_quantity": None,
                                    "upcoming_price": None}
    shop.line_total_overrides[BOTTLE_19L] = 80000.0
    shop.quote_extras = {"price_courtesy": None}
    add_address(bot, ADDRESS, "Uy", "Chilonzor 15")

    await fill_cart(bot, user, BOTTLE_19L, quantity=4)
    await bot.send(user.tap("cart_view"))
    cart = bot.telegram.last_shown().text
    await reach_confirmation(bot, user, ADDRESS, payment="cash")
    confirm = bot.telegram.last_shown().text

    for text in (cart, confirm):
        assert "80,000" in text
        assert "💙" not in text and "📅" not in text
        assert "(" not in text and " + " not in text
    assert_no_swallowed_crash(bot)


async def test_during_the_notice_the_cart_and_confirm_state_the_coming_price_per_line(bot, monkeypatch, user):
    shop = _shop(bot, monkeypatch, upcoming=None)
    quantity = kept_price_shop(shop, extras={**KEPT, "upcoming_price": dict(UPCOMING)})
    add_address(bot, ADDRESS, "Uy", "Chilonzor 15")

    await fill_cart(bot, user, BOTTLE_19L, quantity=quantity)
    await bot.send(user.tap("cart_view"))
    cart_lines = bot.telegram.last_shown().text.split("\n")
    at = cart_lines.index("   " + split_line())
    assert cart_lines[at + 1] == "   " + _line("uz", "20,000 so'm"), "the split first, the dated line second"

    await reach_confirmation(bot, user, ADDRESS, payment="cash")
    confirm_lines = bot.telegram.last_shown().text.split("\n")
    at = confirm_lines.index("   " + html.escape(split_line(), quote=False))
    assert confirm_lines[at + 1] == "   " + html.escape(_line("uz", "20,000 so'm"), quote=False)
    assert_no_swallowed_crash(bot)


async def test_the_order_details_of_a_courtesy_order_show_the_split_and_the_charged_total(bot, user):
    order = {
        "id": 7300, "order_number": "TG_7300", "status": "pending", "total_amount": 76000.0,
        "created_at": "2027-01-20T09:15:00+00:00",
        "order_items": [{
            "product_id": BOTTLE_19L, "product_name": "Aqua Element 19L", "quantity": 4,
            "unit_price": 20000.0, "total_price": 76000.0, **{k: v for k, v in KEPT.items() if k != "unit_price"},
        }],
    }
    bot.backend.route("GET", "/api/v1/orders/7300", lambda _c: {"data": {"order": order, "delivery": None}})

    await bot.send(user.tap("order_7300"))

    text = bot.telegram.last_shown().text
    lines = text.split("\n")
    at = next(i for i, line in enumerate(lines) if "Aqua Element 19L" in line)
    assert "76,000" in lines[at + 1]
    assert lines[at + 2] == "   " + escape_markdown(split_line(), version=2)
    assert "80,000" not in text
    assert_no_swallowed_crash(bot)
