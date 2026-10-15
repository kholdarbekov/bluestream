"""A contract customer is told their contract price applies at checkout (spec 2026-10-09 §4.4, D8).

The server decides (`contract_price_at_checkout`); the bot only renders one line.
"""

import pytest

from tests.telegram_bot.ptb_harness import build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CATEGORY_ID = 1
CONTRACT_ID = 7
PLAIN_ID = 8
HINT = "🤝 Shartnoma narxingiz buyurtmani rasmiylashtirishda qo'llanadi"

TRANSLATIONS = {
    ("uz", "telegram.products.contract_price_at_checkout"): HINT,
    ("uz", "telegram.products.in_stock"): "Mavjud",
    ("uz", "telegram.products.out_of_stock"): "Tugagan",
    ("uz", "telegram.products.stock_label"): "Holat",
    ("uz", "telegram.price"): "Narx",
    ("uz", "telegram.products.volume_label"): "Hajm",
    ("uz", "telegram.products.category_label"): "Turkum",
    ("uz", "telegram.back"): "Orqaga",
    ("uz", "telegram.product.add_to_cart"): "Savatga qo'shish",
    ("uz", "telegram.cart_title"): "Savat",
    ("uz", "telegram.cart_total"): "Jami",
}


def _product(product_id, name, flagged):
    return {
        "id": product_id,
        "name": name,
        "current_price": 15000.0,
        "pricing": {"current_price": 15000.0, "contract_price_at_checkout": flagged},
        "category": {"id": CATEGORY_ID, "name": "Suv"},
        "inventory": {"min_order_quantity": 1, "stock_quantity": 40},
        "specifications": {"volume": 18.9, "volume_unit": "l"},
    }


def _install(backend, catalogue, cart=None):
    backend.route(
        "GET", "/api/v1/products",
        lambda call: {"data": {"items": list(catalogue.values())}, "meta": {"pages": 1}},
    )
    for product_id in catalogue:
        backend.route(
            "GET", f"/api/v1/products/{product_id}",
            lambda call, product_id=product_id: {"data": {"product": catalogue[product_id]}},
        )
    if cart is not None:
        backend.route("GET", "/api/v1/cart", lambda call: {"data": {"cart": cart}})


async def _bot(monkeypatch, catalogue, cart=None):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    _install(harness.backend, catalogue, cart)
    return harness


def _shown(bot):
    call = bot.telegram.last_shown()
    return call.params.get("text") or call.params.get("caption") or ""


async def test_the_product_list_carries_the_hint_once_when_a_product_is_flagged(monkeypatch):
    bot = await _bot(monkeypatch, {
        CONTRACT_ID: _product(CONTRACT_ID, "Aqua Element", True),
        PLAIN_ID: _product(PLAIN_ID, "Aqua Sport", False),
    })
    await bot.send(bot.updates().tap(f"category_{CATEGORY_ID}"))
    assert _shown(bot).count(HINT) == 1, _shown(bot)


async def test_the_product_list_carries_no_hint_without_a_flagged_product(monkeypatch):
    bot = await _bot(monkeypatch, {PLAIN_ID: _product(PLAIN_ID, "Aqua Sport", False)})
    await bot.send(bot.updates().tap(f"category_{CATEGORY_ID}"))
    assert HINT not in _shown(bot)


@pytest.mark.parametrize("product_id, flagged", [(CONTRACT_ID, True), (PLAIN_ID, False)])
async def test_the_product_details_carry_the_hint_only_for_a_flagged_product(monkeypatch, product_id, flagged):
    bot = await _bot(monkeypatch, {
        CONTRACT_ID: _product(CONTRACT_ID, "Aqua Element", True),
        PLAIN_ID: _product(PLAIN_ID, "Aqua Sport", False),
    })
    await bot.send(bot.updates().tap(f"product_{product_id}"))
    assert (HINT in _shown(bot)) is flagged, _shown(bot)


async def test_the_cart_carries_the_hint_when_the_server_flags_it(monkeypatch):
    cart = {
        "cart_items": [{
            "id": 5001,
            "product_id": CONTRACT_ID,
            "quantity": 2,
            "total_price": 30000.0,
            "product": _product(CONTRACT_ID, "Aqua Element", True),
        }],
        "subtotal": 30000.0,
        "contract_price_at_checkout": True,
    }
    bot = await _bot(monkeypatch, {CONTRACT_ID: _product(CONTRACT_ID, "Aqua Element", True)}, cart=cart)
    await bot.send(bot.updates().tap("cart_view"))
    assert HINT in _shown(bot), _shown(bot)
