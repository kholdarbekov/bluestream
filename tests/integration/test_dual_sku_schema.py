"""Dual-SKU ProductGroup JSON-LD.

When a lead asks an LLM "which water size should I get / what does Aqua Element
sell?", the two SKUs must be presented as ONE product family with an explicit
decision rule (cooler -> 18.9 L, no cooler -> 10 L) so the assistant can
reproduce the recommendation and surface BOTH sizes. These tests pin that the
homepage and shop emit a schema.org ProductGroup with both variants + an
AggregateOffer spanning the real price range, and that incidental pages don't.
"""
import json
import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from business_app.models.product import Product, ProductCategory
from business_app.models.translation import Translation


@pytest.fixture
def client(app):
    # Function-scoped client: the session-scoped default leaks session['language']
    # across tests in an xdist worker and the language before_request then
    # 302-redirects '/', making these asserts order-dependent. (See project
    # test-suite cookie-leak gotcha.)
    return app.test_client()


@pytest.fixture
def two_skus(db):
    cat = ProductCategory(name="Water", description="Water products", is_active=True)
    db.session.add(cat)
    db.session.commit()
    p19 = Product(
        name="Aqua Element 18.9L",
        description="19L returnable cooler bottle",
        category_id=cat.id,
        size="19L",
        volume=18.9,
        volume_unit="L",
        base_price=Decimal("22000.00"),
        stock_quantity=100,
        is_active=True,
        slug="aqua-element-18-9l",
        sku="AE-19",
        created_at=datetime.now(UTC),
    )
    p10 = Product(
        name="Aqua Element 10L",
        description="10L bottle",
        category_id=cat.id,
        size="10L",
        volume=10.0,
        volume_unit="L",
        base_price=Decimal("14000.00"),
        stock_quantity=100,
        is_active=True,
        slug="aqua-element-10l",
        sku="AE-10",
        created_at=datetime.now(UTC),
    )
    db.session.add_all([p19, p10])
    # /shop renders 'landing.shop.results_count'|t|format(a, b, c); seed it so the
    # page renders in the test DB (missing key -> placeholder-less string -> the
    # format filter raises TypeError, unrelated to the schema under test).
    for lang in ("uz", "ru", "en"):
        db.session.add(
            Translation(
                key="landing.shop.results_count",
                language=lang,
                value="Showing %s-%s of %s products",
                category="landing",
            )
        )
    db.session.commit()
    return p19, p10


def _find_product_group(html):
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    for raw in blocks:
        try:
            data = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("@type") == "ProductGroup":
            return data
    return None


@pytest.mark.integration
class TestDualSkuProductGroup:
    def test_index_emits_product_group_with_both_skus(self, client, two_skus, db):
        r = client.get("/")
        assert r.status_code == 200
        pg = _find_product_group(r.get_data(as_text=True))
        assert pg is not None, "homepage must emit a ProductGroup linking both SKUs"

        sizes = {v["size"] for v in pg["hasVariant"]}
        assert any("18.9" in s for s in sizes), f"missing 18.9 L variant in {sizes}"
        assert any("10" in s for s in sizes), f"missing 10 L variant in {sizes}"

        # Explicit, LLM-reproducible decision rule.
        assert "cooler" in pg["description"].lower()

        # AggregateOffer spans the real price range of both SKUs.
        offers = pg["offers"]
        assert offers["@type"] == "AggregateOffer"
        assert float(offers["lowPrice"]) == 14000.0
        assert float(offers["highPrice"]) == 22000.0
        assert offers["offerCount"] == 2
        assert offers["priceCurrency"] == "UZS"

    def test_each_variant_carries_returnable_and_cooler_flags(self, client, two_skus, db):
        r = client.get("/")
        pg = _find_product_group(r.get_data(as_text=True))
        by_size = {}
        for v in pg["hasVariant"]:
            props = {p["name"]: p["value"] for p in v.get("additionalProperty", [])}
            by_size[v["size"]] = props
        big = next(props for size, props in by_size.items() if "18.9" in size)
        small = next(props for size, props in by_size.items() if "10" in size)
        assert big["Returnable bottle"] is True
        assert big["Requires a cooler/dispenser"] is True
        assert small["Returnable bottle"] is False
        assert small["Requires a cooler/dispenser"] is False

    def test_shop_emits_product_group(self, client, two_skus, db):
        r = client.get("/shop")
        assert r.status_code == 200
        pg = _find_product_group(r.get_data(as_text=True))
        assert pg is not None
        assert pg["@type"] == "ProductGroup"
        assert pg["variesBy"] == ["https://schema.org/size"]

    def test_incidental_page_has_no_product_group(self, client, two_skus, db):
        r = client.get("/contact")
        assert r.status_code == 200
        assert _find_product_group(r.get_data(as_text=True)) is None
