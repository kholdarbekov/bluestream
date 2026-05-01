"""Regression coverage for ledger-backed admin financial summary reporting."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app import db
from business_app.models.delivery import Delivery
from business_app.models.order import OrderStatus
from business_app.services.admin_report_service import AdminReportService
from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import DeliveryStatus, PaymentMethod
@pytest.mark.unit
def test_financial_summary_uses_cash_collection_ledger_for_cod(app, sample_order, sample_user, delivery_driver):
    with app.app_context():
        sample_order.status = OrderStatus.DELIVERED
        sample_order.payment_method = PaymentMethod.CASH
        delivery = Delivery(
            order_id=sample_order.id,
            delivery_person_id=delivery_driver.id,
            status=DeliveryStatus.DELIVERED,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="09:00-12:00",
            actual_delivery_time=datetime.now(UTC),
            delivered_at=datetime.now(UTC),
        )
        db.session.add(delivery)
        db.session.commit()

        cash_service = CashCollectionService()
        cash_service.ensure_cod_payment_for_order(sample_order)
        cash_service.post_collection(
            customer_id=sample_user.id,
            amount=Decimal("5000.00"),
            source="delivery_completion",
            collector_user_id=delivery_driver.id,
            recorded_by_user_id=delivery_driver.id,
            order_id=sample_order.id,
            delivery_id=delivery.id,
            notes="Collected part of the COD balance",
            occurred_at=datetime.now(UTC),
        )

        start_dt = datetime.now(UTC) - timedelta(days=1)
        end_dt = datetime.now(UTC) + timedelta(days=1)
        report = AdminReportService.generate("financial_summary", start_dt, end_dt, {})

        assert report["summary"]["total_cash_collected"] == 5000.0
        assert report["summary"]["total_revenue"] == 5000.0
        assert report["summary"]["delivered_order_revenue"] == float(sample_order.total_amount)
        assert report["summary"]["outstanding_cod_total"] == float(sample_order.total_amount - Decimal("5000.00"))
        assert any(item["method"] == "cash" and item["amount"] == 5000.0 for item in report["payment_method_breakdown"])
        assert report["cash_collection_source_breakdown"][0]["source"] == "delivery_completion"
