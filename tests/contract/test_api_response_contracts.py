"""Contract tests for common API response envelope shapes."""

import pytest


def _assert_base_envelope(body):
    assert isinstance(body, dict)
    assert "success" in body
    assert isinstance(body["success"], bool)


@pytest.mark.integration
@pytest.mark.api
class TestAPIResponseContracts:
    def test_products_list_success_contract(self, client, sample_product):
        response = client.get("/api/v1/products/")
        assert response.status_code == 200

        body = response.get_json()
        _assert_base_envelope(body)
        assert body["success"] is True
        assert "data" in body
        assert "items" in body["data"]

    def test_product_details_success_contract(self, client, sample_product):
        response = client.get(f"/api/v1/products/{sample_product.id}")
        assert response.status_code == 200

        body = response.get_json()
        _assert_base_envelope(body)
        assert body["success"] is True
        assert "data" in body
        assert "product" in body["data"]

    def test_login_error_contract(self, client, db, sample_user):
        response = client.post(
            "/api/v1/auth/login",
            json={"identifier": sample_user.email, "password": "wrong-password"},
        )
        assert response.status_code in [400, 401, 423, 429]

        body = response.get_json()
        _assert_base_envelope(body)
        assert body["success"] is False
        assert "message" in body or "errors" in body

    def test_not_found_contract(self, client, db):
        response = client.get("/api/v1/products/999999")
        assert response.status_code == 404

        body = response.get_json()
        _assert_base_envelope(body)
        assert body["success"] is False
        assert "message" in body
