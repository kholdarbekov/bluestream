"""Paging a category's product list, driven the way a customer pages it.

THE DEFECT THIS FILE WAS WRITTEN FOR
------------------------------------
``ProductKeyboards.product_list`` renders Previous/Next whenever the backend
reports more than one page, and ``ProductHandlers`` feeds it the real
``meta.pages`` from ``GET /api/v1/products`` (asked for six at a time). So every
category with more than six active products has shipped these two buttons to
every customer — and nothing was registered for them. The tap reached no
handler, no ``answerCallbackQuery`` was sent, and the spinner stopped only when
Telegram gave up. Silent, and invisible in the logs.

WHY THE CALLBACK CARRIES THE CATEGORY
-------------------------------------
The old callback was ``page_{n}``: it named the page and nothing else, so a
handler could only have recovered the category from ``context.user_data`` — and
the Application is built with no ``persistence``, so bot memory is empty after
every deploy and after every restart. A product list left open across a deploy
would page into nothing. The category (and the single-category Back target that
goes with it) therefore rides on the CALLBACK, exactly like the order id on the
cancel-confirmation card (``handlers/orders.py::_cancel_confirmation_callback``).

Every test below taps through the REAL dispatcher, and the ones that matter tap
a button on a bot whose memory has never been written to — which is the only
way to prove the category came off the callback rather than out of a dict that
happens to still be warm.
"""

import math

import pytest

# Bot modules resolve by bare name; tests/telegram_bot/conftest.py ranks
# telegram_bot/ first on sys.path.
from keyboards import product_page_callback

from tests.telegram_bot.ptb_harness import build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


CATEGORY_ID = 3
# `_render_products_in_category` asks the backend for six products at a time;
# the fixture below serves whatever page size is actually requested, so this
# number is documentation, not a second copy of the rule.
PRODUCTS_PER_PAGE = 6

TRANSLATIONS = {
    ("uz", "telegram.back"): "Orqaga",
    ("uz", "telegram.menu.products"): "Mahsulotlar",
    ("uz", "telegram.pagination.previous"): "Oldingi",
    ("uz", "telegram.pagination.next"): "Keyingi",
    ("uz", "telegram.products.category_empty"): "Bu turkumda mahsulot yo'q",
    ("uz", "telegram.products.invalid_action"): "Bu tugma eskirgan",
    ("uz", "telegram.error_occurred"): "Xatolik yuz berdi",
}


def _product(number: int) -> dict:
    """One catalogue row, named so that ``"Suv 07" in text`` is unambiguous."""
    return {
        "id": number,
        "name": f"Suv {number:02d}",
        "current_price": 15000.0,
        "inventory": {"min_order_quantity": 1, "stock_quantity": 40},
        "specifications": {"volume": 19, "volume_unit": "l"},
    }


# 14 products = three pages of six.
CATALOGUE = [_product(number) for number in range(1, 15)]


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    harness.requested_pages = []

    def serve_products(call):
        params = call.params or {}
        page = int(params.get("page", 1))
        per_page = int(params.get("per_page", PRODUCTS_PER_PAGE))
        harness.requested_pages.append((str(params.get("category_id")), page))
        start = (page - 1) * per_page
        return {
            "data": {"items": CATALOGUE[start:start + per_page]},
            "meta": {"pages": math.ceil(len(CATALOGUE) / per_page)},
        }

    harness.backend.route("GET", "/api/v1/products", serve_products)
    harness.backend.route(
        "GET",
        "/api/v1/products/categories",
        lambda call: {
            "data": {"categories": [{"id": CATEGORY_ID, "name": "Suv"}, {"id": 4, "name": "Sharbat"}]}
        },
    )
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


def paging_buttons(call) -> list[str]:
    return [data for data in call.callback_data() if data.startswith("page_")]


def toasts(bot) -> list[str]:
    return [
        call.params["text"]
        for call in bot.telegram.of("answerCallbackQuery")
        if "text" in call.params
    ]


async def test_the_next_button_pages_the_category_the_customer_is_looking_at(bot, user):
    """The whole loop: open a category, tap Next, land on page two.

    Driven through the dispatcher rather than by calling the builder, because
    the builder can only emit a page button for a category it was TOLD about —
    a handler that forgets to pass one renders no pagination at all, and a test
    that calls the builder itself would never notice.
    """
    await bot.send(user.tap(f"category_{CATEGORY_ID}"))

    first_page = bot.telegram.last_shown()
    assert "Suv 01" in first_page.text and "Suv 07" not in first_page.text, (
        "the first screen is not page one of the category"
    )
    assert paging_buttons(first_page) == [product_page_callback(CATEGORY_ID, 2)], (
        "page one of a three-page category must offer exactly one Next button, "
        f"addressed at this category; got {first_page.callback_data()}"
    )

    bot.telegram.reset()
    bot.requested_pages.clear()

    await bot.send(user.tap(product_page_callback(CATEGORY_ID, 2)))

    assert bot.requested_pages == [(str(CATEGORY_ID), 2)], (
        "the Next tap did not fetch page two of this category: "
        f"{bot.requested_pages}"
    )
    second_page = bot.telegram.last_shown()
    assert "Suv 07" in second_page.text, "page two never reached the customer"
    assert "Suv 01" not in second_page.text, "the customer is still looking at page one"
    assert bot.telegram.of("answerCallbackQuery"), (
        "the tap was never acked — the spinner runs until Telegram gives up"
    )
    assert paging_buttons(second_page) == [
        product_page_callback(CATEGORY_ID, 1),
        product_page_callback(CATEGORY_ID, 3),
    ], f"the middle page must offer both directions; got {second_page.callback_data()}"


async def test_the_previous_button_walks_back_to_the_first_page(bot, user):
    await bot.send(user.tap(product_page_callback(CATEGORY_ID, 3)))
    bot.telegram.reset()
    bot.requested_pages.clear()

    await bot.send(user.tap(product_page_callback(CATEGORY_ID, 2)))
    bot.telegram.reset()
    bot.requested_pages.clear()

    await bot.send(user.tap(product_page_callback(CATEGORY_ID, 1)))

    assert bot.requested_pages == [(str(CATEGORY_ID), 1)]
    first_page = bot.telegram.last_shown()
    assert "Suv 01" in first_page.text
    assert paging_buttons(first_page) == [product_page_callback(CATEGORY_ID, 2)], (
        "page one still offered a Previous button"
    )


@pytest.mark.parametrize(
    "single_category, expected_back",
    [(False, "back_to_categories"), (True, "back_to_main")],
)
async def test_a_page_button_that_outlived_a_restart_still_pages(
    bot, user, single_category, expected_back
):
    """The card is on screen from before the deploy; the bot remembers nothing.

    This tap is the FIRST update this Application has ever processed for this
    customer, so ``context.user_data`` is empty. Everything the re-render needs
    — which category, which page, and where Back goes — can only have come off
    the callback_data. An implementation that reads any of it out of bot memory
    fails here and passes every other test in this file.
    """
    tap = product_page_callback(CATEGORY_ID, 3, single_category=single_category)

    await bot.send(user.tap(tap))

    assert bot.requested_pages == [(str(CATEGORY_ID), 3)], (
        f"{tap!r} did not re-render page three of category {CATEGORY_ID}: "
        f"{bot.requested_pages}"
    )
    screen = bot.telegram.last_shown()
    assert "Suv 13" in screen.text, "the last page never reached the customer"
    assert expected_back in screen.callback_data(), (
        "the Back target changed when the customer paged; "
        f"expected {expected_back!r} in {screen.callback_data()}"
    )
    assert bot.telegram.of("answerCallbackQuery"), "the tap was never acked"


async def test_a_page_button_rendered_by_an_older_release_says_so(bot, user):
    """``page_2`` — the shape that shipped before this fix.

    It names the page and not the category, so it cannot be re-rendered. A card
    still on a customer's screen during the deploy must therefore TELL them,
    not spin: the one thing that must never happen again is a tap that produces
    no answerCallbackQuery at all.
    """
    await bot.send(user.tap("page_2"))

    assert toasts(bot) == [TRANSLATIONS[("uz", "telegram.products.invalid_action")]], (
        f"the stale card was not answered: toasts={toasts(bot)}"
    )
    assert bot.requested_pages == [], (
        "a callback carrying no category still went and fetched some products"
    )
