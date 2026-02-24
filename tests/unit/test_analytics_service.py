"""
Unit tests for AnalyticsService aligned with current implementation.
"""

from datetime import datetime, UTC, timedelta

import pytest

from business_app.services.analytics_service import AnalyticsService
from business_app.models.analytics import UserBehavior


@pytest.fixture
def analytics_service():
    return AnalyticsService()


@pytest.mark.unit
@pytest.mark.analytics
class TestAnalyticsService:
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

    def test_predict_customer_churn_for_single_user(self, analytics_service, monkeypatch):
        monkeypatch.setattr(analytics_service, "_calculate_user_churn_probability_optimized", lambda _uid: 0.72)

        payload = analytics_service.predict_customer_churn(user_id=99)

        assert payload["user_id"] == 99
        assert payload["churn_probability"] == 0.72
        assert payload["risk_level"] in {"low", "medium", "high"}

    def test_generate_business_report_rejects_unknown_type(self, analytics_service):
        with pytest.raises(ValueError):
            analytics_service.generate_business_report("unsupported")

    def test_track_user_behavior_persists_record(self, analytics_service, db, sample_user):
        analytics_service.track_user_behavior(
            user_id=sample_user.id,
            action="view_dashboard",
            metadata={"ip_address": "127.0.0.1", "user_agent": "pytest"},
        )

        created = db.session.query(UserBehavior).filter_by(user_id=sample_user.id, action="view_dashboard").count()
        assert created == 1
