"""The admin order detail publishes a courtesy line's split (final fix wave, B2).

`GET /api/v1/admin/orders/<id>` carried `quantity`, `unit_price` and `total_price` only, so the Orders page printed
"4 x 20,000" beside a 76,000 total. The items now carry the split `order_item_split` publishes (the one publisher the
order API and the edit history read too); the page prints it (admin_ui/src/__tests__/pages/Orders.courtesySplit.test.js).
"""

import pytest

from business_app.serializers.order_serializers import order_item_split
from tests.integration.price_courtesy_builders import courtesy_world, water
from tests.integration.test_price_courtesy_order_create import SPLIT_KEYS, _only_line, _order, _place

pytestmark = pytest.mark.integration


@pytest.fixture
def client(app):
    """A fresh client per test: the session-scoped one carries JWT cookies from test to test."""
    return app.test_client()


@pytest.fixture
def w(app, db, sample_product, admin_user):
    return courtesy_world(app, db, water(db, sample_product), admin_user)


def test_the_admin_order_detail_items_carry_the_split(client, db, w, admin_claim_headers):
    order = _order(db, _place(client, w.customer, w.product))

    response = client.get(f"/api/v1/admin/orders/{order.id}", headers=admin_claim_headers)

    assert response.status_code == 200, response.get_json()
    body = response.get_json()["data"]
    (item,) = (body.get("order") or body)["items"]
    stored = order_item_split(_only_line(order))
    assert {key: item[key] for key in SPLIT_KEYS} == {key: stored[key] for key in SPLIT_KEYS}
    assert (item["courtesy_quantity"], item["regular_quantity"], item["courtesy_unit_price"]) == (2, 2, 18000.0)
