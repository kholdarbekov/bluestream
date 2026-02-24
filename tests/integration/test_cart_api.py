"""Integration tests for cart API endpoints."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class _CartStub:
    def __init__(self, items=None):
        self.items = items or []

    def to_dict(self):
        return {"items": self.items, "total_items": len(self.items)}


@pytest.fixture
def mocked_cart_service():
    service = MagicMock()
    service.get_cart_by_user_id.return_value = _CartStub([{"product_id": 1, "quantity": 2}])
    service.add_item_to_cart.return_value = _CartStub([{"product_id": 1, "quantity": 3}])
    service.update_item_quantity.return_value = _CartStub([{"product_id": 1, "quantity": 5}])
    service.remove_item_from_cart.return_value = _CartStub([])
    service.sync_cart_from_local.return_value = _CartStub([{"product_id": 2, "quantity": 1}])
    service.calculate_cart_estimate.return_value = {"pricing": {"final_total": 18000}, "validation": {"meets_minimum": True}}
    service.prepare_cart_for_checkout.return_value = {
        "ready_for_checkout": True,
        "items": [
            {
                "product_id": 1,
                "product": SimpleNamespace(name="Pure Water 19L"),
                "quantity": 2,
                "unit_price": 15000,
                "subtotal": 30000,
            }
        ],
        "subtotal": 30000,
        "warnings": [],
    }
    return service


@pytest.mark.integration
@pytest.mark.api
class TestCartAPI:
    def test_get_cart(self, client, auth_headers, mocked_cart_service):
        with patch("business_app.api.carts.get_cart_service", return_value=mocked_cart_service):
            response = client.get("/api/v1/cart/", headers=auth_headers)

        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        assert body["data"]["cart"]["total_items"] == 1
        mocked_cart_service.get_cart_by_user_id.assert_called_once()

    def test_add_update_remove_item(self, client, auth_headers, mocked_cart_service):
        with patch("business_app.api.carts.get_cart_service", return_value=mocked_cart_service):
            add = client.post("/api/v1/cart/items", json={"product_id": 1, "quantity": 3}, headers=auth_headers)
            update = client.put("/api/v1/cart/items/1", json={"quantity": 5}, headers=auth_headers)
            remove = client.delete("/api/v1/cart/items/1", headers=auth_headers)

        assert add.status_code == 200
        assert update.status_code == 200
        assert remove.status_code == 200
        assert remove.get_json()["data"]["cart"]["total_items"] == 0

    def test_clear_and_sync_cart(self, client, auth_headers, mocked_cart_service):
        with patch("business_app.api.carts.get_cart_service", return_value=mocked_cart_service):
            clear = client.post("/api/v1/cart/clear", headers=auth_headers)
            invalid_sync = client.post("/api/v1/cart/sync", json={"cart_items": "bad"}, headers=auth_headers)
            valid_sync = client.post(
                "/api/v1/cart/sync",
                json={"cart_items": [{"product_id": 2, "quantity": 1}]},
                headers=auth_headers,
            )

        assert clear.status_code == 200
        assert clear.get_json()["success"] is True

        assert invalid_sync.status_code == 400
        assert invalid_sync.get_json()["success"] is False

        assert valid_sync.status_code == 200
        assert valid_sync.get_json()["data"]["cart"]["total_items"] == 1

    def test_estimate_and_validate_cart(self, client, auth_headers, mocked_cart_service):
        with patch("business_app.api.carts.get_cart_service", return_value=mocked_cart_service):
            estimate = client.post(
                "/api/v1/cart/estimate",
                json={"cart_items": [{"product_id": 1, "quantity": 2}], "delivery_address_id": 1},
                headers=auth_headers,
            )
            validate = client.post(
                "/api/v1/cart/validate",
                json={"cart_items": [{"product_id": 1, "quantity": 2}]},
                headers=auth_headers,
            )

        assert estimate.status_code == 200
        assert estimate.get_json()["data"]["estimate"]["pricing"]["final_total"] == 18000

        assert validate.status_code == 200
        validate_body = validate.get_json()["data"]
        assert validate_body["valid"] is True
        assert validate_body["items"][0]["product_name"] == "Pure Water 19L"
        assert validate_body["subtotal"] == 30000
