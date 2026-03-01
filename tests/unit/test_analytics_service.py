"""
Unit tests for AnalyticsService aligned with current implementation.
"""

from datetime import datetime, UTC, timedelta
from decimal import Decimal

import pytest

from business_app.models.order import Order, OrderItem
from business_app.services.analytics_service import AnalyticsService
from business_app.models.analytics import UserBehavior
from business_app.models.user import UserAddress
from business_app.utils.constants import OrderStatus, PaymentMethod


@pytest.fixture
def analytics_service():
    return AnalyticsService()


@pytest.mark.unit
@pytest.mark.analytics
class TestAnalyticsService:
    def test_customer_acquisition_metrics_handles_absent_referral_column(self, analytics_service, sample_user):
        payload = analytics_service._get_customer_acquisition_metrics(
            datetime.now(UTC) - timedelta(days=1),
            datetime.now(UTC) + timedelta(days=1),
        )

        assert payload["total_new_customers"] >= 1
        assert payload["referred_customers"] == 0

    def test_dashboard_overview_uses_aggregated_sections(self, analytics_service, monkeypatch):
        monkeypatch.setattr(analytics_service, "_get_revenue_metrics", lambda *_: {"total_revenue": 10})
        monkeypatch.setattr(analytics_service, "_get_order_metrics", lambda *_: {"total_orders": 2})
        monkeypatch.setattr(analytics_service, "_get_customer_metrics", lambda *_: {"active_customers": 3})
        monkeypatch.setattr(analytics_service, "_get_delivery_metrics", lambda *_: {"on_time": 90})
        monkeypatch.setattr(analytics_service, "_get_growth_trends", lambda *_: {"growth_rate": 5})

        payload = analytics_service.get_dashboard_overview()

        assert "revenue" in payload
        assert "orders" in payload
        assert "customers" in payload
        assert "delivery" in payload
        assert "growth" in payload

    def test_get_sales_analytics_returns_expected_sections(self, analytics_service, monkeypatch):
        monkeypatch.setattr(analytics_service, "_get_daily_sales_trend", lambda *_: [])
        monkeypatch.setattr(analytics_service, "_get_product_performance", lambda *_: [])
        monkeypatch.setattr(analytics_service, "_get_hourly_sales_distribution", lambda *_: [])
        monkeypatch.setattr(analytics_service, "_get_weekly_sales_distribution", lambda *_: [])
        monkeypatch.setattr(analytics_service, "_get_geographic_sales_distribution", lambda *_: [])
        monkeypatch.setattr(analytics_service, "_get_customer_segment_analysis", lambda *_: {})

        payload = analytics_service.get_sales_analytics()

        assert set(payload.keys()) == {
            "daily_trends",
            "product_performance",
            "hourly_distribution",
            "weekly_distribution",
            "geographic_distribution",
            "customer_segments",
        }

    def test_get_product_analytics_uses_explicit_product_order_joins(self, analytics_service, db, sample_user, sample_product):
        address = UserAddress(
            user_id=sample_user.id,
            title='Home',
            full_address='Street 1',
            street_address='Street 1',
            city='Tashkent',
            is_default=True,
        )
        db.session.add(address)
        db.session.flush()

        order = Order(
            order_number='ORD-AN-1',
            user_id=sample_user.id,
            status=OrderStatus.DELIVERED,
            subtotal=Decimal('15000.00'),
            total_amount=Decimal('15000.00'),
            delivery_fee=Decimal('0.00'),
            delivery_address_id=address.id,
            payment_method=PaymentMethod.CASH,
            created_at=datetime.now(UTC),
        )
        db.session.add(order)
        db.session.flush()

        order_item = OrderItem(
            order_id=order.id,
            product_id=sample_product.id,
            quantity=2,
            unit_price=Decimal('7500.00'),
            discount_amount=Decimal('0.00'),
            total_price=Decimal('15000.00'),
        )
        db.session.add(order_item)
        db.session.commit()

        payload = analytics_service.get_product_analytics(
            datetime.now(UTC) - timedelta(days=1),
            datetime.now(UTC) + timedelta(days=1),
            limit=10,
        )

        assert payload[0]["product_id"] == sample_product.id
        assert payload[0]["quantity_sold"] == 2
        assert payload[0]["revenue"] == 15000.0

    def test_get_customer_analytics_returns_expected_sections(self, analytics_service, monkeypatch):
        monkeypatch.setattr(analytics_service, "_get_customer_acquisition_metrics", lambda *_: {})
        monkeypatch.setattr(analytics_service, "_get_customer_retention_metrics", lambda *_: {})
        monkeypatch.setattr(analytics_service, "_get_customer_lifetime_value_analysis", lambda *_: {})
        monkeypatch.setattr(analytics_service, "_get_customer_churn_analysis", lambda *_: {})
        monkeypatch.setattr(analytics_service, "_get_customer_behavior_patterns", lambda *_: {})

        payload = analytics_service.get_customer_analytics()

        assert set(payload.keys()) == {
            "acquisition",
            "retention",
            "lifetime_value",
            "churn",
            "behavior_patterns",
        }

    def test_get_delivery_analytics_returns_expected_sections(self, analytics_service, monkeypatch):
        monkeypatch.setattr(analytics_service, "_get_delivery_performance_metrics", lambda *_: {}, raising=False)
        monkeypatch.setattr(analytics_service, "_get_route_efficiency_metrics", lambda *_: {})
        monkeypatch.setattr(analytics_service, "_get_driver_performance_metrics", lambda *_: {})
        monkeypatch.setattr(analytics_service, "_get_delivery_geographic_patterns", lambda *_: {})

        payload = analytics_service.get_delivery_analytics()

        assert set(payload.keys()) == {
            "performance",
            "route_efficiency",
            "driver_performance",
            "geographic_patterns",
        }

    def test_geographic_sales_distribution_uses_delivery_address_city(self, analytics_service, db, sample_user):
        address = UserAddress(
            user_id=sample_user.id,
            title='Office',
            full_address='Business district',
            street_address='Business district',
            city='Samarkand',
            is_default=True,
        )
        db.session.add(address)
        db.session.flush()

        order = Order(
            order_number='ORD-GEO-1',
            user_id=sample_user.id,
            status=OrderStatus.DELIVERED,
            subtotal=Decimal('10000.00'),
            total_amount=Decimal('10000.00'),
            delivery_fee=Decimal('0.00'),
            delivery_address_id=address.id,
            payment_method=PaymentMethod.CASH,
            created_at=datetime.now(UTC),
        )
        db.session.add(order)
        db.session.commit()

        payload = analytics_service._get_geographic_sales_distribution(
            datetime.now(UTC) - timedelta(days=1),
            datetime.now(UTC) + timedelta(days=1),
        )

        assert payload[0]["city"] == 'Samarkand'
        assert payload[0]["orders"] == 1

    def test_predict_customer_churn_batch_returns_summary(self, analytics_service, sample_user, monkeypatch):
        monkeypatch.setattr(analytics_service, "_get_batch_user_statistics", lambda _ids: {
            sample_user.id: {
                "last_order_date": datetime.now(UTC) - timedelta(days=10),
                "order_count": 3,
                "avg_order_value": Decimal('10000.00'),
                "total_deliveries": 2,
                "failed_deliveries": 0,
            }
        })
        monkeypatch.setattr(analytics_service, "_calculate_churn_from_stats", lambda *_: 0.5)

        payload = analytics_service.predict_customer_churn()

        assert "predictions" in payload
        assert "high_risk_customers" in payload
        assert "medium_risk_customers" in payload

    def test_predict_demand_returns_error_when_history_is_insufficient(self, analytics_service, monkeypatch):
        monkeypatch.setattr(analytics_service, "_get_historical_demand_data", lambda: [{"date": "2026-01-01", "order_count": 3}] * 5)

        payload = analytics_service.predict_demand(7)

        assert "error" in payload
        assert payload["min_days_required"] == 30

    def test_predict_demand_returns_predictions_for_sufficient_data(self, analytics_service, monkeypatch):
        start = datetime.now(UTC).date() - timedelta(days=45)
        history = []
        for day in range(45):
            history.append({
                "date": (start + timedelta(days=day)).isoformat(),
                "order_count": 20 + (day % 7),
            })

        monkeypatch.setattr(analytics_service, "_get_historical_demand_data", lambda: history)

        payload = analytics_service.predict_demand(5)

        assert "predictions" in payload
        assert len(payload["predictions"]) == 5
        assert "model_accuracy" in payload

    def test_predict_revenue_returns_forecast_for_sufficient_data(self, analytics_service, monkeypatch):
        start = datetime.now(UTC).date() - timedelta(days=45)
        history = []
        for day in range(45):
            history.append({
                "date": (start + timedelta(days=day)).isoformat(),
                "order_count": 20 + (day % 7),
                "revenue": 1000 + (day * 25),
            })

        monkeypatch.setattr(analytics_service, "_get_historical_demand_data", lambda: history)

        payload = analytics_service.predict_revenue(10)

        assert "predictions" in payload
        assert len(payload["predictions"]) == 10
        assert payload["next_month_revenue"] >= 0
        assert "confidence_level" in payload

    def test_predict_customer_churn_for_single_user(self, analytics_service, monkeypatch):
        monkeypatch.setattr(analytics_service, "_calculate_user_churn_probability_optimized", lambda _uid: 0.72)

        payload = analytics_service.predict_customer_churn(user_id=99)

        assert payload["user_id"] == 99
        assert payload["churn_probability"] == 0.72
        assert payload["risk_level"] in {"low", "medium", "high"}

    def test_generate_business_report_rejects_unknown_type(self, analytics_service):
        with pytest.raises(ValueError):
            analytics_service.generate_business_report("unsupported")

    def test_delivery_performance_metrics_alias_uses_builder(self, analytics_service, monkeypatch):
        monkeypatch.setattr(
            analytics_service,
            "_build_delivery_performance_metrics",
            lambda *_: {"success_rate": 98.0},
        )

        payload = analytics_service._get_delivery_performance_metrics(datetime.now(UTC), datetime.now(UTC))

        assert payload == {"success_rate": 98.0}

    def test_track_user_behavior_persists_record(self, analytics_service, db, sample_user):
        analytics_service.track_user_behavior(
            user_id=sample_user.id,
            action="view_dashboard",
            metadata={"ip_address": "127.0.0.1", "user_agent": "pytest"},
        )

        created = db.session.query(UserBehavior).filter_by(user_id=sample_user.id, action="view_dashboard").count()
        assert created == 1
