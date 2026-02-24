"""Regression tests for previously fragile edge-case behaviors."""

from unittest.mock import MagicMock, patch

import pytest

from business_app.utils.order_validators import sanitize_search_query, validate_order_query_params
from business_app.utils.security_validators import SecurityValidator


@pytest.mark.unit
class TestValidatorRegressions:
    def test_search_sanitizer_handles_non_string_input(self):
        assert sanitize_search_query(None) == ""
        assert sanitize_search_query(123) == ""

    def test_query_param_validator_handles_non_integer_inputs(self):
        errors = validate_order_query_params({"page": "abc", "per_page": "xyz"})
        assert errors["page"] == ["Page must be a valid integer"]
        assert errors["per_page"] == ["Per page must be a valid integer"]

    def test_validate_all_user_fields_sanitizes_names_in_place(self):
        payload = {"first_name": "  John<script> ", "last_name": "Doe", "company_name": " ACME & Co "}
        errors = SecurityValidator.validate_all_user_fields(payload)

        assert errors == []
        assert payload["first_name"] == "Johnscript"
        assert payload["company_name"] == "ACME  Co"

    def test_outbound_http_is_blocked_by_global_test_guard(self):
        import requests

        with pytest.raises(RuntimeError, match="Outbound HTTP blocked during tests"):
            requests.get("https://example.com", timeout=1)


@pytest.mark.integration
@pytest.mark.api
class TestAPIRegressions:
    def test_cart_sync_with_empty_list_returns_successful_shape(self, client, auth_headers):
        fake_service = MagicMock()
        fake_service.sync_cart_from_local.return_value = None

        with patch("business_app.api.carts.get_cart_service", return_value=fake_service):
            response = client.post("/api/v1/cart/sync", json={"cart_items": []}, headers=auth_headers)

        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        assert "cart" in body["data"]
        assert body["data"]["cart"] is None
