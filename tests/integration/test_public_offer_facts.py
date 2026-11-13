"""The offer facts: one service states what Aqua Element sells, at what price, in what smallest order, with what
delivery fee, and every public surface prints from it (plan 2026-11-12, Task 1).

The unit price is the one an order is charged (`Product.catalogue_unit_price`); the smallest order clears both the
product's own minimum and the order floor, and the order endpoint accepts it while the quote refuses one less. Raw
facts keep the money formatter's no-break spaces, so expected strings write them as `\\u00a0`. The prices are unusual
(17 345) so a price typed anywhere cannot pass.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from flask import url_for

from business_app.models.translation import Translation
from business_app.services.public_offer_service import (
    DELIVERY_FEE_KEY,
    FREE_DELIVERY_KEY,
    build_offer_jsonld,
    get_public_offer_facts,
    product_offer_facts,
    product_public_path,
    public_price_range,
)
from business_app.utils import display_labels
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from tests.integration.contract_pricing_factory import customer_headers
from tests.integration.public_page_builders import LANGS, offer_skus, seed_copy, set_price  # noqa: F401 -- a fixture
from tests.integration.sales_pay_builders import freeze_local_now
from tests.integration.tier_discount_factory import post_order, verify_phone

pytestmark = pytest.mark.integration


def _facts_for(product) -> dict:
    """`product`'s entry in the published facts."""
    return next(p for p in get_public_offer_facts()["products"] if p["id"] == product.id)


def _put(db, key: str, value: str) -> None:
    """One test-owned row for `key` in every site language."""
    for language in LANGS:
        db.session.add(Translation(key=key, language=language, value=value, category="landing", is_active=True))
    db.session.commit()


def test_unit_price_is_what_an_order_is_charged(app, db, offer_skus):
    seed_copy(db, ("web.display_labels",))
    set_price(db, offer_skus.p19, base=Decimal("17345"))
    p = _facts_for(offer_skus.p19)
    assert p["unit_price"] == offer_skus.p19.catalogue_unit_price == Decimal("17345")
    assert p["list_price"] is None and p["list_price_text"] is None and p["discount_percent"] is None
    assert p["price_text"] == {
        "uz": "17\u00a0345\u00a0so'm",
        "ru": "17\u00a0345\u00a0сум",
        "en": "17\u00a0345\u00a0so'm",
    }


def test_a_real_discount_shows_both_prices(app, db, offer_skus):
    seed_copy(db, ("web.display_labels",))
    set_price(db, offer_skus.p19, base=Decimal("17345"), discount=Decimal("15000"))
    p = _facts_for(offer_skus.p19)
    assert (p["unit_price"], p["list_price"], p["discount_percent"]) == (Decimal("15000"), Decimal("17345"), 13)
    assert p["list_price_text"]["ru"] == "17\u00a0345\u00a0сум"


@pytest.mark.parametrize(
    "discount, charged", [(Decimal("0"), Decimal("17345")), (Decimal("20000"), Decimal("20000"))]
)
def test_a_discount_that_is_not_one_shows_no_list_price(app, db, offer_skus, discount, charged):  # Review Focus 1
    set_price(db, offer_skus.p19, base=Decimal("17345"), discount=discount)
    p = _facts_for(offer_skus.p19)
    assert p["unit_price"] == offer_skus.p19.catalogue_unit_price == charged and p["list_price"] is None


def test_smallest_order_satisfies_the_quantity_rule_and_the_amount_floor(app, db, offer_skus, monkeypatch):
    monkeypatch.setitem(app.config, "MIN_ORDER_AMOUNT", 20000)
    assert _facts_for(offer_skus.p19)["smallest_order_quantity"] == 2  # the product minimum decides
    monkeypatch.setitem(app.config, "MIN_ORDER_AMOUNT", 50000)
    set_price(db, offer_skus.p19, base=Decimal("17345"))  # 2 x 17 345 is under the floor
    big, small = _facts_for(offer_skus.p19), _facts_for(offer_skus.p10)
    assert (big["smallest_order_quantity"], big["smallest_order_total"]) == (3, Decimal("52035"))
    assert (small["smallest_order_quantity"], small["smallest_order_total"]) == (5, Decimal("61700"))
    assert get_public_offer_facts()["min_order_amount"] == Decimal("50000")


def test_the_published_smallest_order_is_accepted_and_one_less_is_refused(
    app, db, offer_skus, sample_user, user_address
):
    verify_phone(db, sample_user)
    floor = app.config["MIN_ORDER_AMOUNT"]  # read, not patched: both endpoints use the real floor
    set_price(db, offer_skus.p19, base=Decimal(floor // 3 - 1))  # three bottles stay under the floor
    qty = product_offer_facts(offer_skus.p19)["smallest_order_quantity"]
    assert qty > offer_skus.p19.min_order_quantity
    headers = customer_headers(app, sample_user)
    placed = post_order(
        app, headers, product_id=offer_skus.p19.id, address_id=user_address.id, payment_method="cash", quantity=qty
    )
    assert placed.status_code == 201, placed.get_json()
    refused = app.test_client().post(
        "/api/v1/cart/estimate",
        headers=headers,
        json={
            "cart_items": [{"product_id": offer_skus.p19.id, "quantity": qty - 1}],
            "payment_method": "cash",
            "delivery_address_id": user_address.id,
        },
    )
    assert refused.status_code == 400 and refused.get_json()["error_code"] == "ORDER_MIN_AMOUNT"


def test_a_zero_price_does_not_raise(app, db, offer_skus):
    set_price(db, offer_skus.p19, base=Decimal("0"))
    p = product_offer_facts(offer_skus.p19)
    assert (p["smallest_order_quantity"], p["smallest_order_total"]) == (2, Decimal("0"))


def test_size_text_names_the_litres_and_the_trade_size(app, db, offer_skus, sample_product):
    seed_copy(db, ("web.display_labels",))
    big, small = _facts_for(offer_skus.p19), _facts_for(offer_skus.p10)
    assert (big["litres"], big["size_code"], big["sku"]) == (Decimal("18.9"), "19L", "AE-19")
    assert big["size_text"] == {
        "uz": "18,9\u00a0l (19\u00a0l)",
        "ru": "18,9\u00a0л (19\u00a0л)",
        "en": "18.9\u00a0L (19\u00a0L)",
    }
    assert small["size_text"] == {"uz": "10\u00a0l", "ru": "10\u00a0л", "en": "10\u00a0L"}
    # volume 19.0 is the trade size: no bracket
    assert product_offer_facts(sample_product)["size_text"]["en"] == "19\u00a0L"


def test_no_deposit_follows_the_returnable_flag_and_stock_decides_availability(app, db, offer_skus):
    big, small = _facts_for(offer_skus.p19), _facts_for(offer_skus.p10)
    flags = (big["returnable"], big["no_deposit"], small["returnable"], small["no_deposit"])
    assert flags == (True, True, False, False)
    assert big["available"] is True
    offer_skus.p19.stock_quantity = 0
    db.session.commit()
    assert _facts_for(offer_skus.p19)["available"] is False


def test_only_active_products_are_listed_dearest_first(app, db, offer_skus):
    assert [p["id"] for p in get_public_offer_facts()["products"]] == [offer_skus.p19.id, offer_skus.p10.id]
    offer_skus.p19.is_active = False
    db.session.commit()
    assert [p["id"] for p in get_public_offer_facts()["products"]] == [offer_skus.p10.id]
    assert product_offer_facts(offer_skus.p19)["unit_price"] == Decimal("23450")  # any product, active or not


def test_the_product_path_is_the_one_the_routes_serve(app, db, offer_skus):
    assert _facts_for(offer_skus.p19)["path"] == f"/product/{offer_skus.p19.id}"
    offer_skus.p10.slug = "aqua-element-10l"
    with app.test_request_context():
        assert product_public_path(offer_skus.p19) == url_for("frontend.product_detail", product_id=offer_skus.p19.id)
        assert product_public_path(offer_skus.p10) == url_for("frontend.product_detail_slug", slug="aqua-element-10l")


def test_as_of_is_today_in_tashkent(app, db, monkeypatch):
    freeze_local_now(monkeypatch, datetime(2026, 11, 12, 20, 30, tzinfo=UTC))  # 01:30 on the 13th in Tashkent
    assert get_public_offer_facts()["as_of"] == "2026-11-13"


def test_the_delivery_line_follows_the_fee(app, db, monkeypatch):  # Review Focus 2; no money row seeded
    _put(db, FREE_DELIVERY_KEY, "free-line")
    _put(db, DELIVERY_FEE_KEY, "fee-line {fee}")
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", 0)
    d = get_public_offer_facts()["delivery"]
    assert (d["fee"], d["free"], d["line"]["ru"]) == (Decimal("0"), True, "free-line")
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", 5000)
    d = get_public_offer_facts()["delivery"]
    assert (d["fee"], d["free"], d["fee_text"]["ru"]) == (Decimal("5000"), False, "5\u00a0000\u00a0UZS")
    assert d["line"]["ru"] == "fee-line 5\u00a0000\u00a0UZS"


def test_money_and_litres_without_their_rows_print_the_amount(app, db, offer_skus):  # Review Focus 3
    assert display_labels.money(17345, "uz") == "17\u00a0345\u00a0UZS"
    assert display_labels.litres(Decimal("18.9"), "ru") == "18,9\u00a0L"
    assert display_labels.litres(10.0, "en") == "10\u00a0L"
    assert _facts_for(offer_skus.p19)["price_text"]["uz"] == "23\u00a0450\u00a0UZS"


def test_the_litres_row_is_seeded_like_the_money_row():
    assert BACKEND_TRANSLATIONS["web.display_labels.litres"] == {
        "en": "{amount}\u00a0L",
        "uz": "{amount}\u00a0l",
        "ru": "{amount}\u00a0л",
    }


def test_no_product_and_no_copy_still_gives_facts(app, db):
    facts = get_public_offer_facts()
    assert (facts["products"], facts["currency"], public_price_range()) == ([], "UZS", None)


def test_price_range_text(app, db, offer_skus):
    assert public_price_range() == "UZS 12340-23450"
    set_price(db, offer_skus.p10, base=Decimal("23450"))
    assert public_price_range() == "UZS 23450"


def test_offer_jsonld_is_plain_json_and_states_no_delivery_time(app, db, offer_skus, monkeypatch):
    # No `shippingDetails`: Google's merchant-listing guide makes `deliveryTime` a required property of
    # `OfferShippingDetails`, and this site states no delivery time in JSON-LD (plan Task 1, Step 3).
    monkeypatch.setitem(app.config, "MIN_ORDER_AMOUNT", 20000)
    set_price(db, offer_skus.p19, base=Decimal("17345"))
    facts = get_public_offer_facts()
    offer = build_offer_jsonld(facts["products"][0], url="https://x.test/product/1?lang=ru")
    assert offer == {
        "@type": "Offer",
        "priceCurrency": "UZS",
        "price": 17345,
        "availability": "https://schema.org/InStock",
        "url": "https://x.test/product/1?lang=ru",
        "eligibleQuantity": {"@type": "QuantitativeValue", "minValue": 2},
    }
    assert type(offer["price"]) is int and "deliveryTime" not in json.dumps(offer)
    set_price(db, offer_skus.p19, base=Decimal("17345.50"))
    facts = get_public_offer_facts()
    offer = build_offer_jsonld(facts["products"][0], url="https://x.test/p")
    assert offer["price"] == 17345.5 and type(offer["price"]) is float
