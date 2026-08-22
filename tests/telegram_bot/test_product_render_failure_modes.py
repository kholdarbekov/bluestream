"""When Telegram refuses the edit, the customer must still get the screen.

THE CLASS
---------
A product screen renders with a bare ``query.edit_message_text(...)``. Telegram
answers it in two routine ways that both appear in this project's production
logs:

* ``Message is not modified`` — benign. The bubble ALREADY shows exactly this
  content; there is nothing to do and nothing is wrong.
* ``Message to edit not found`` — the customer deleted the bubble, or it aged
  out. The content still needs to reach them, in a NEW message.

Both used to escape into the handler's blanket ``except``, which turns a
RENDERING problem into a FLOW problem: ``_handle_error`` fires and the customer
gets a generic error toast — and in the "not found" case no product list, no
product card, nothing to tap. A dead end produced by a message bubble.

``BaseHandler._edit_or_replace_callback_message`` already exists for exactly
this: "not modified" is success, anything else falls back to replacing the
message. These tests drive the real dispatcher with the real Telegram
rejections and assert on what the customer ends up looking at.
"""

import pytest

from tests.telegram_bot.ptb_harness import build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


CATEGORY_ID = 3
PRODUCT_ID = 11
PRODUCT_NAME = "Aqua Element"

TRANSLATIONS = {
    ("uz", "telegram.products.in_stock"): "Mavjud",
    ("uz", "telegram.products.out_of_stock"): "Tugagan",
    ("uz", "telegram.products.stock_label"): "Holat",
    ("uz", "telegram.products.category_empty"): "Bu turkumda mahsulot yo'q",
    ("uz", "telegram.price"): "Narx",
    ("uz", "telegram.products.volume_label"): "Hajm",
    ("uz", "telegram.products.category_label"): "Turkum",
    ("uz", "telegram.back"): "Orqaga",
    ("uz", "telegram.product.add_to_cart"): "Savatga qo'shish",
    ("uz", "telegram.error_occurred"): "Xatolik yuz berdi",
}

PRODUCT = {
    "id": PRODUCT_ID,
    "name": PRODUCT_NAME,
    "current_price": 15000.0,
    "category": {"id": CATEGORY_ID, "name": "Suv"},
    "inventory": {"min_order_quantity": 1, "stock_quantity": 40},
    "specifications": {"volume": 18.9, "volume_unit": "l"},
}


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    harness.backend.route(
        "GET",
        "/api/v1/products",
        lambda call: {"data": {"items": [PRODUCT]}, "meta": {"pages": 1}},
    )
    harness.backend.route(
        "GET",
        f"/api/v1/products/{PRODUCT_ID}",
        lambda call: {"data": {"product": PRODUCT}},
    )
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


def error_toasts(bot) -> list[str]:
    return [
        call.params["text"]
        for call in bot.telegram.of("answerCallbackQuery")
        if "text" in call.params
    ]


def fresh_messages(bot) -> list[str]:
    return [call.params.get("text", "") for call in bot.telegram.of("sendMessage")]


@pytest.mark.parametrize(
    "tap, screen_name",
    [
        (f"category_{CATEGORY_ID}", "the product list"),
        (f"product_{PRODUCT_ID}", "the product card"),
    ],
)
async def test_a_deleted_bubble_still_gets_the_screen_in_a_new_message(
    bot, user, tap, screen_name
):
    """"Message to edit not found" — the bubble is gone, the screen is not.

    The content has to arrive in a fresh message. An error toast and an empty
    chat is the customer stranded with nothing to tap.
    """
    bot.telegram.fail("editMessageText", "Message to edit not found", status=400)

    await bot.send(user.tap(tap))

    sent = fresh_messages(bot)
    assert any(PRODUCT_NAME.split(" ")[1] in text for text in sent), (
        f"{screen_name} never reached the customer after the edit was refused; "
        f"sent={sent} toasts={error_toasts(bot)}"
    )
    assert TRANSLATIONS[("uz", "telegram.error_occurred")] not in error_toasts(bot), (
        "a rendering problem was reported to the customer as a failure"
    )


@pytest.mark.parametrize(
    "tap", [f"category_{CATEGORY_ID}", f"product_{PRODUCT_ID}"]
)
async def test_an_unchanged_screen_is_not_an_error(bot, user, tap):
    """"Message is not modified" means the customer is already looking at it.

    Nothing to re-send, and above all nothing to apologise for.
    """
    bot.telegram.fail("editMessageText", "Message is not modified", status=400)

    await bot.send(user.tap(tap))

    assert TRANSLATIONS[("uz", "telegram.error_occurred")] not in error_toasts(bot)
    assert fresh_messages(bot) == [], (
        "an unchanged screen was re-sent as a duplicate message"
    )


async def test_an_empty_category_still_reports_itself_when_the_edit_is_refused(
    bot, user
):
    """The empty-category fallback renders through the same bare edit."""
    bot.backend.route(
        "GET", "/api/v1/products", lambda call: {"data": {"items": []}, "meta": {"pages": 1}}
    )
    bot.telegram.fail("editMessageText", "Message to edit not found", status=400)

    await bot.send(user.tap(f"category_{CATEGORY_ID}"))

    assert TRANSLATIONS[("uz", "telegram.products.category_empty")] in fresh_messages(bot)


async def test_the_category_picker_still_arrives_when_the_edit_is_refused(bot, user):
    """``products_menu`` hand-rolled its own edit/fallback instead of using the
    shared helper — a second expression of the same rule, and one that treats
    the benign "not modified" as a failure worth deleting the bubble over."""
    bot.backend.route(
        "GET",
        "/api/v1/products/categories",
        lambda call: {"data": {"categories": [{"id": CATEGORY_ID, "name": "Suv"}, {"id": 4, "name": "Sharbat"}]}},
    )
    bot.telegram.fail("editMessageText", "Message is not modified", status=400)

    await bot.send(user.tap("menu_products"))

    assert fresh_messages(bot) == [], (
        "the category picker was re-sent as a duplicate over a benign rejection"
    )
    assert TRANSLATIONS[("uz", "telegram.error_occurred")] not in error_toasts(bot)
