"""Unit tests for extracted admin service-layer entry points."""

import pytest

from business_app.services.admin_bulk_action_service import AdminBulkActionService
from business_app.services.admin_report_service import AdminReportService
from business_app.utils.exceptions import ValidationError


def test_admin_report_service_rejects_unknown_report_type():
    with pytest.raises(ValidationError):
        AdminReportService.validate_report_type("unknown_report")


def test_admin_report_service_accepts_known_report_type():
    # Should not raise
    AdminReportService.validate_report_type("sales_summary")


def test_admin_bulk_action_service_exposes_expected_targets_and_actions():
    valid_actions = AdminBulkActionService.get_valid_actions()

    assert "user" in valid_actions
    assert "order" in valid_actions
    assert "product" in valid_actions
    assert "review" in valid_actions
    assert "subscription" in valid_actions
    assert "delivery" in valid_actions
    assert "activate" in valid_actions["user"]
    assert "assign_role" in valid_actions["user"]
    assert "mark_delivered" in valid_actions["order"]


def test_admin_bulk_action_service_is_valid_action_helper():
    assert AdminBulkActionService.is_valid_action("user", "activate") is True
    assert AdminBulkActionService.is_valid_action("user", "invalid") is False
    assert AdminBulkActionService.is_valid_action("invalid", "activate") is False
