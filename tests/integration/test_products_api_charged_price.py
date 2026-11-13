"""The public products API prices a product the way an order is charged (final review P4; owner, 2026-11-13).

`GET /api/v1/products/` and `GET /api/v1/products/<id>` feed the customer bot's product list and product screen (with
the customer's token), and `POST /api/v1/products/bulk` feeds the website cart page (`static/js/pages/cart.js`, signed
out, body `{product_ids, language}`). Each publishes `pricing.current_price`. It starts from
`Product.catalogue_unit_price`, the figure an order is charged and the server cart starts from, so a discount price
reaches the bot and the cart page as it reaches the site, the feeds and the order. A discount of 0, or one above the
base price, is no discount (Review Focus 1): the charged figure, nothing marked discounted, never a negative amount.
The prices are unusual (17 345) so a typed price cannot pass.
"""

from decimal import Decimal

import pytest

from tests.integration.contract_pricing_factory import customer_headers
from tests.integration.public_page_builders import offer_skus, set_price  # noqa: F401 -- a fixture

pytestmark = pytest.mark.integration

BASE = Decimal("17345")
SURFACES = ("bot-list", "bot-product", "cart-page")


def _served_pricing(app, user, product, surface: str) -> dict:
    """`product`'s `pricing` as the named client receives it, off the real route."""
    client = app.test_client()
    if surface == "bot-list":  # `api_client.get_products(category=...)`
        response = client.get(
            "/api/v1/products/",
            query_string={"page": 1, "per_page": 20, "category_id": product.category_id},
            headers=customer_headers(app, user),
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        items = response.get_json()["data"]["items"]
        return next(item for item in items if item["id"] == product.id)["pricing"]
    if surface == "bot-product":  # `api_client.get_product(product_id)`
        response = client.get(f"/api/v1/products/{product.id}", headers=customer_headers(app, user))
        assert response.status_code == 200, response.get_data(as_text=True)
        return response.get_json()["data"]["product"]["pricing"]
    # cart.js `loadCart`: a signed-out visitor's cart page
    response = client.post("/api/v1/products/bulk", json={"product_ids": [product.id], "language": "uz"})
    assert response.status_code == 200, response.get_data(as_text=True)
    [served] = response.get_json()["data"]["products"]
    return served["pricing"]


@pytest.mark.parametrize("surface", SURFACES)
def test_a_discount_price_is_what_the_bot_and_the_cart_page_are_served(app, db, offer_skus, sample_user, surface):
    set_price(db, offer_skus.p19, base=BASE, discount=Decimal("15000"))
    charged = float(offer_skus.p19.catalogue_unit_price)
    assert charged == 15000.0

    pricing = _served_pricing(app, sample_user, offer_skus.p19, surface)

    assert pricing["current_price"] == charged
    assert pricing["base_price"] == float(BASE)
    assert pricing["is_discounted"] is True
    assert pricing["discount_amount"] == float(BASE) - charged
    assert pricing["discount_percentage"] == round((float(BASE) - charged) / float(BASE) * 100, 2)
    assert pricing["total_price"] == charged


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("discount", [None, Decimal("0"), Decimal("20000")], ids=["none", "zero", "above-base"])
def test_no_discount_or_one_that_is_not_one_publishes_the_charged_price_undiscounted(
    app, db, offer_skus, sample_user, surface, discount
):
    """No discount: nothing differs from before. A discount of 0 is ignored by the charge, so the base price is
    published; one above the base IS charged, so it is published, and neither is marked discounted."""
    set_price(db, offer_skus.p19, base=BASE, discount=discount)
    charged = float(offer_skus.p19.catalogue_unit_price)

    pricing = _served_pricing(app, sample_user, offer_skus.p19, surface)

    assert pricing["current_price"] == charged
    assert pricing["is_discounted"] is False
    assert pricing["discount_amount"] == 0
    assert pricing["discount_percentage"] == 0


def test_search_suggestions_quote_the_charged_price(app, db, offer_skus):
    set_price(db, offer_skus.p19, base=BASE, discount=Decimal("15000"))
    response = app.test_client().get(
        "/api/v1/products/search-suggestions", query_string={"q": offer_skus.p19.sku}
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    [suggestion] = response.get_json()["data"]["suggestions"]
    assert suggestion["price"] == float(offer_skus.p19.catalogue_unit_price) == 15000.0


def test_the_price_calculator_answers_not_found_for_a_deactivated_product(app, db, offer_skus):
    """A deactivated product is "not found" to the public (owner, 2026-11-13), the calculator included: no price."""
    set_price(db, offer_skus.p19, base=BASE, discount=Decimal("15000"))
    client = app.test_client()
    payload = {"product_id": offer_skus.p19.id, "quantity": 2}

    active = client.post("/api/v1/products/price-calculator", json=payload)
    assert active.status_code == 200, active.get_data(as_text=True)
    priced = active.get_json()["data"]
    assert priced["unit_price"] == float(offer_skus.p19.catalogue_unit_price) == 15000.0
    assert priced["subtotal"] == 30000.0

    offer_skus.p19.is_active = False
    db.session.commit()
    gone = client.post("/api/v1/products/price-calculator", json=payload)
    missing = client.post("/api/v1/products/price-calculator", json={**payload, "product_id": 99999999})
    assert gone.status_code == missing.status_code == 404
    assert gone.get_json()["success"] is False
    assert set(gone.get_json()) == set(missing.get_json())
    for price_key in ("base_price", "discount_price", "unit_price", "subtotal"):
        assert price_key not in gone.get_data(as_text=True)

    offer_skus.p19.is_active = True
    db.session.commit()
    again = client.post("/api/v1/products/price-calculator", json=payload)
    assert again.status_code == 200
    assert again.get_json()["data"]["unit_price"] == 15000.0
