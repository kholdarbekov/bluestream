"""Unit tests for order input validator utilities."""

from datetime import UTC, date, datetime, timedelta

import pytest

from business_app.utils.constants import PaymentMethod
from business_app.utils.order_validators import (
    OrderInputValidator,
    sanitize_search_query,
    validate_export_params,
    validate_order_query_params,
)


def _valid_order_payload():
    return {
        "items": [{"product_id": 1, "quantity": 2}],
        "delivery_address_id": 10,
        "delivery_date": (date.today() + timedelta(days=1)).isoformat(),
        "delivery_time_slot": "09:00-12:00",
        "payment_method": PaymentMethod.CARD.value,
        "loyalty_points_used": 0,
        "promo_code": "DISCOUNT10",
        "delivery_notes": "Ring once",
        "order_source": "web",
        "is_urgent": False,
    }


@pytest.mark.unit
@pytest.mark.order
class TestOrderInputValidator:
    def test_validate_create_order_success(self):
        validator = OrderInputValidator()
        errors = validator.validate_create_order(_valid_order_payload())
        assert errors == {}

    def test_validate_create_order_required_and_item_errors(self):
        validator = OrderInputValidator()
        errors = validator.validate_create_order({"items": [], "delivery_address_id": "abc"})

        assert "items" in errors
        assert "delivery_address_id" in errors

    def test_validate_create_order_rejects_invalid_optional_fields(self):
        validator = OrderInputValidator()
        data = _valid_order_payload()
        data["promo_code"] = "BAD CODE!"
        data["delivery_notes"] = "javascript:alert(1)"
        data["order_source"] = "desktop"
        data["is_urgent"] = "yes"
        data["loyalty_points_used"] = -1

        errors = validator.validate_create_order(data)

        assert "promo_code" in errors
        assert "delivery_notes" in errors
        assert "order_source" in errors
        assert "is_urgent" in errors
        assert "loyalty_points_used" in errors

    def test_validate_emergency_order_requires_non_cash_payment(self):
        validator = OrderInputValidator()
        data = {
            "items": [{"product_id": 1, "quantity": 1}],
            "delivery_address_id": 1,
            "payment_method": PaymentMethod.CASH.value,
        }
        errors = validator.validate_emergency_order(data)

        assert "payment_method" in errors
        assert any("not allowed" in message for message in errors["payment_method"])

    def test_validate_subscription_and_scheduled_order_paths(self):
        validator = OrderInputValidator()

        subscription_errors = validator.validate_subscription_order(
            {
                "items": [{"product_id": 1, "quantity": 1}],
                "frequency": "yearly",
                "delivery_address_id": 1,
                "start_date": (date.today() - timedelta(days=5)).isoformat(),
                "auto_pay": "true",
            }
        )
        assert "frequency" in subscription_errors
        assert "start_date" in subscription_errors
        assert "auto_pay" in subscription_errors

        scheduled_errors = validator.validate_scheduled_order(
            {
                "items": [{"product_id": 1, "quantity": 1}],
                "scheduled_date": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                "delivery_address_id": 1,
                "payment_method": PaymentMethod.CARD.value,
            }
        )
        assert "scheduled_date" in scheduled_errors

    def test_validate_bulk_feedback_export_and_filters(self):
        validator = OrderInputValidator()

        bulk_errors = validator.validate_bulk_action({"action": "bad", "order_ids": [1, -2, "x"]})
        assert "action" in bulk_errors
        assert "order_ids" in bulk_errors

        feedback_errors = validator.validate_order_feedback({"rating": "7", "comment": "javascript:bad()"})
        assert "rating" in feedback_errors
        assert "comment" in feedback_errors

        export_errors = validator.validate_export(
            {
                "format": "json",
                "start_date": "invalid",
                "end_date": "invalid",
                "filters": {"status": "x" * 101},
            }
        )
        assert "format" in export_errors
        assert "start_date" in export_errors
        assert "end_date" in export_errors
        assert "filters" in export_errors


@pytest.mark.unit
@pytest.mark.order
class TestOrderValidatorHelpers:
    def test_validate_order_query_params(self):
        errors = validate_order_query_params(
            {
                "page": "0",
                "per_page": "500",
                "status": "not-a-status",
                "start_date": "2026-01-10",
                "end_date": "2026-01-01",
            }
        )

        assert "page" in errors
        assert "per_page" in errors
        assert "status" in errors
        assert "date_range" in errors

    def test_sanitize_search_query_strips_sql_and_limits_length(self):
        query = "<script>alert(1)</script> UNION SELECT * FROM users; DROP TABLE orders -- " + ("a" * 500)
        cleaned = sanitize_search_query(query)

        assert "<script>" not in cleaned.lower()
        assert "union select" not in cleaned.lower()
        assert "drop table" not in cleaned.lower()
        assert len(cleaned) == 200

    def test_validate_export_params(self):
        missing = validate_export_params({})
        assert missing["format"] == ["Format is required"]

        invalid = validate_export_params(
            {
                "format": "json",
                "start_date": "bad",
                "end_date": "bad",
                "filters": "not-a-dict",
            }
        )
        assert "format" in invalid
        assert "start_date" in invalid
        assert "end_date" in invalid
        assert "filters" in invalid

        valid = validate_export_params(
            {
                "format": "csv",
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
                "filters": {"status": "pending"},
            }
        )
        assert valid == {}
