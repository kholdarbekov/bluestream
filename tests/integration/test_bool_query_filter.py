"""Integration coverage for the parse_bool_arg fix.

Flask's ``request.args.get(name, type=bool)`` runs Python's ``bool()`` on the raw
query string, so ``bool("false") is True`` — any falsy string value was treated as
True. This test hits a real endpoint (products listing, in_stock_only filter) with
``in_stock_only=false`` and asserts the filter is treated as OFF (out-of-stock
products still returned). It fails against the old ``type=bool`` code and passes
post-fix.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from business_app.models.product import Product


@pytest.mark.integration
@pytest.mark.api
def test_in_stock_only_false_does_not_restrict_results(client, db, sample_product, sample_category):
    out_of_stock_product = Product(
        name="Sold Out Water 19L",
        description="Temporarily out of stock",
        category_id=sample_category.id,
        size="19L",
        volume=19.0,
        volume_unit="L",
        base_price=Decimal("15000.00"),
        stock_quantity=0,
        track_inventory=True,
        min_stock_level=10,
        max_stock_level=500,
        is_active=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(out_of_stock_product)
    db.session.commit()

    response = client.get("/api/v1/products/?in_stock_only=false&per_page=100")

    assert response.status_code == 200
    body = response.get_json()
    returned_ids = {item["id"] for item in body["data"]["items"]}

    # With the bug, bool("false") == True would drop the out-of-stock product.
    assert out_of_stock_product.id in returned_ids
    assert sample_product.id in returned_ids
