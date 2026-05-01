"""Unit tests for admin bulk action order cancellation flow."""

from unittest.mock import patch

from business_app.services.admin_bulk_action_service import AdminBulkActionService
from shared.enums import OrderStatus
def test_bulk_order_cancel_delegates_to_order_service(sample_order, admin_user):
    sample_order.status = OrderStatus.PENDING

    with patch("business_app.services.order_service.OrderService.cancel_order") as cancel_order:
        result = AdminBulkActionService.perform(
            action="cancel",
            target_type="order",
            target_ids=[sample_order.id],
            parameters={},
            reason="Admin cancelled order",
            admin_id=admin_user.id,
        )

    assert result["success_count"] == 1
    assert result["failed_count"] == 0
    cancel_order.assert_called_once_with(
        sample_order.id,
        reason="Admin cancelled order",
        actor_user_id=admin_user.id,
    )
