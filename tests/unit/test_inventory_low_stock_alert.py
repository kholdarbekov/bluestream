"""
Unit test for InventoryService._send_low_stock_alert dispatching the correct Celery task.

Regression test for a wrong import: the method imported ``send_low_stock_alert_task``
from ``business_app.tasks.notification_tasks`` (where it does not exist) instead of
``business_app.tasks.inventory_tasks`` (where it is actually defined), causing the
surrounding try/except to silently swallow an ImportError and never dispatch the task.
"""

from unittest.mock import patch

import pytest

from business_app.services.inventory_service import InventoryService


@pytest.mark.unit
@pytest.mark.inventory
class TestSendLowStockAlert:
    def test_send_low_stock_alert_dispatches_inventory_task(self, app, sample_product):
        with app.app_context():
            with patch("business_app.tasks.inventory_tasks.send_low_stock_alert_task.delay") as mock_delay:
                InventoryService()._send_low_stock_alert(sample_product)

                mock_delay.assert_called_once_with(sample_product.id)
