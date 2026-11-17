"""The bulk "update_price" action is gone (spec 5.13).

It used to answer success while writing nothing. Now the admin bulk-actions route refuses it and the service no longer
offers a price edit for products. Each test drives the route the admin UI's bulk menu calls, with the body it sends.
"""

from decimal import Decimal

import pytest

from business_app.models.product import Product
from business_app.services.admin_bulk_action_service import AdminBulkActionService

pytestmark = pytest.mark.integration

BULK_ACTIONS = "/api/v1/admin/bulk-actions"


def test_the_bulk_price_action_is_refused_and_writes_nothing(app, db, sample_product, admin_auth_headers):
    client = app.test_client()
    body = {
        "action": "update_price",
        "target_type": "product",
        "target_ids": [sample_product.id],
        "parameters": {"new_price": 20000},
    }

    response = client.post(BULK_ACTIONS, json=body, headers=admin_auth_headers)

    assert response.status_code == 400
    assert any('Invalid action "update_price"' in error for error in response.get_json()["errors"])
    db.session.expire_all()
    assert db.session.get(Product, sample_product.id).base_price == Decimal("15000.00")


def test_the_product_bulk_actions_no_longer_offer_a_price_edit():
    assert AdminBulkActionService.get_valid_actions()["product"] == ["activate", "deactivate", "delete", "update_stock"]
