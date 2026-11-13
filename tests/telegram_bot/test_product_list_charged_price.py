"""The customer bot's product list prints the price an order is charged (final review P4; owner, 2026-11-13).

The bot's `GET /api/v1/products` call is answered by the real Flask route with the customer's own token, so the list
is drawn from exactly what the real serializer returns for the product. A discount price set in admin reaches the bot
as it reaches the site, the feeds and the order (`Product.catalogue_unit_price`). The prices are unusual (17 345) so
a typed price cannot pass.
"""

from decimal import Decimal

import pytest

# `ptb_harness` first: it caches the BOT's `config`/`i18n`/`utils` before `public_page_builders` imports
# `scripts.seed_backend_translations`, which puts the repo root back at `sys.path[0]`
# (see tests/telegram_bot/test_track_on_the_way_dispatcher.py).
from tests.telegram_bot.ptb_harness import build_bot_harness
from utils import format_price

from tests.integration.contract_pricing_factory import customer_headers
from tests.integration.public_page_builders import offer_skus, set_price  # noqa: F401 -- a fixture

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

BASE = Decimal("17345")


async def test_the_product_list_prints_the_discount_price_the_order_charges(
    monkeypatch, app, db, offer_skus, sample_user
):
    set_price(db, offer_skus.p19, base=BASE, discount=Decimal("15000"))
    charged = float(offer_skus.p19.catalogue_unit_price)
    bot = await build_bot_harness(monkeypatch)
    served: list[dict] = []

    def real_route(call):
        response = app.test_client().get(
            "/api/v1/products/", query_string=call.params, headers=customer_headers(app, sample_user)
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        served.append(next(p for p in response.get_json()["data"]["items"] if p["id"] == offer_skus.p19.id))
        return response.get_json()

    bot.backend.route("GET", "/api/v1/products", real_route)

    await bot.send(bot.updates().tap(f"category_{offer_skus.p19.category_id}"))

    [product] = served
    assert product["pricing"]["current_price"] == charged == 15000.0
    shown = bot.telegram.last_shown()
    assert f"💰 {format_price(charged)} UZS" in shown.text, shown.text
    assert f"{product['name']} - {product['pricing']['current_price']} UZS" in shown.button_labels()
    for listed in (shown.text, *shown.button_labels()):
        assert format_price(float(BASE)) not in listed and str(float(BASE)) not in listed, listed
