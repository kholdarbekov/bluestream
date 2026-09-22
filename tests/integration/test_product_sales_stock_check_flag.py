"""in_sales_stock_check must be READABLE on every product surface a client uses.

The write path (admin create/update + the Products.js switch) is task 7. This
file pins the read side, because the switch cannot render its current state
from a row that never publishes the field: the admin Products page reads
GET /api/v1/admin/products, and the storefront reads GET /api/v1/products/<id>.
"""

import pytest

from business_app import db
from business_app.models.product import Product

pytestmark = pytest.mark.integration


def _flag(product_id, value):
    """Set the flag through the SAME session the test client's request will use.

    A nested ``app.app_context()`` here would open a SECOND session whose commit
    the request never sees. The ``db`` fixture (tests/conftest.py:168) yields
    from inside an app context it pushed itself, and Flask reuses an
    already-pushed context for a test-client request rather than pushing a new
    one — so the handler runs on the ``db`` fixture's session, the same session
    that created ``sample_product`` and still holds it in its identity map. A
    second session's commit would land in the database but never reach that
    cached instance. Writing through this session (and letting commit expire it)
    is what makes the round trip real.
    """
    product = Product.query.get(product_id)
    product.in_sales_stock_check = value
    db.session.commit()


def test_admin_listing_publishes_the_flag_defaulting_to_false(app, admin_auth_headers, sample_product):
    product_id = sample_product.id

    resp = app.test_client().get("/api/v1/admin/products", headers=admin_auth_headers)

    assert resp.status_code == 200, resp.get_json()
    row = next(item for item in resp.get_json()["data"]["items"] if item["id"] == product_id)
    assert row["in_sales_stock_check"] is False


def test_admin_listing_publishes_the_flag_once_set(app, admin_auth_headers, sample_product):
    product_id = sample_product.id
    _flag(product_id, True)

    resp = app.test_client().get("/api/v1/admin/products", headers=admin_auth_headers)

    assert resp.status_code == 200, resp.get_json()
    row = next(item for item in resp.get_json()["data"]["items"] if item["id"] == product_id)
    assert row["in_sales_stock_check"] is True


def test_public_product_detail_publishes_the_flag_in_inventory(app, sample_product):
    product_id = sample_product.id
    _flag(product_id, True)

    resp = app.test_client().get(f"/api/v1/products/{product_id}")

    assert resp.status_code == 200, resp.get_json()
    inventory = resp.get_json()["data"]["product"]["inventory"]
    assert inventory["in_sales_stock_check"] is True


def test_product_to_dict_publishes_the_flag(app, sample_product):
    product_id = sample_product.id

    with app.app_context():
        assert Product.query.get(product_id).to_dict()["in_sales_stock_check"] is False
