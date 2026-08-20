"""Unit tests for order input validator utilities."""

from datetime import date, timedelta

import pytest

from shared.enums import PaymentMethod
from business_app.utils.delivery_window import local_now
from shared.business_config import MAX_SCHEDULE_HORIZON_DAYS
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

    def test_delivery_date_rule_is_the_shared_one_not_a_local_copy(self):
        """This validator used to re-derive the past/horizon rule against
        `date.today()` — the container clock, not the operator-local one the
        endpoints validate with — so the two disagreed for five hours every
        evening. It now delegates to `delivery_window.validate_schedule`.
        Reverting it to a local copy would have to reproduce these exact
        messages, which is the point: there is one rule, and this is its wording.
        """
        today = local_now().date()

        past = _valid_order_payload()
        past["delivery_date"] = (today - timedelta(days=1)).isoformat()
        assert OrderInputValidator().validate_create_order(past)["delivery_date"] == [
            "delivery_date cannot be in the past"
        ]

        too_far = _valid_order_payload()
        too_far["delivery_date"] = (today + timedelta(days=MAX_SCHEDULE_HORIZON_DAYS + 2)).isoformat()
        assert OrderInputValidator().validate_create_order(too_far)["delivery_date"] == [
            f"delivery_date cannot be more than {MAX_SCHEDULE_HORIZON_DAYS} days in the future"
        ]

        on_the_horizon = _valid_order_payload()
        on_the_horizon["delivery_date"] = (today + timedelta(days=MAX_SCHEDULE_HORIZON_DAYS)).isoformat()
        assert "delivery_date" not in OrderInputValidator().validate_create_order(on_the_horizon)

    def test_delivery_date_still_reports_its_own_shape_errors(self):
        """Parsing stays local: the shared rule answers "is this schedule
        allowed", not "is this field even a date"."""
        bad_type = _valid_order_payload()
        bad_type["delivery_date"] = 20260820
        assert OrderInputValidator().validate_create_order(bad_type)["delivery_date"] == [
            "delivery_date must be a string"
        ]

        bad_format = _valid_order_payload()
        bad_format["delivery_date"] = "20-08-2026"
        assert OrderInputValidator().validate_create_order(bad_format)["delivery_date"] == [
            "delivery_date must be in ISO format (YYYY-MM-DD)"
        ]

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

    def test_validate_subscription_order_paths(self):
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
