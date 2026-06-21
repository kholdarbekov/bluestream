"""Regression: analytics report metrics must be JSON-serializable.

Prod bug: the daily analytics beat task crashed with
``TypeError: Object of type Decimal is not JSON serializable`` when persisting
``AnalyticsReport.report_data`` (a JSON column), because ``_get_revenue_metrics``
and ``_get_customer_lifetime_value_analysis`` returned raw ``Decimal`` money
values (every sibling method already wraps money in ``float()``).
"""

import json

from datetime import datetime, timedelta, UTC

import pytest

from business_app.services.analytics_service import AnalyticsService


@pytest.fixture
def analytics_service():
    return AnalyticsService()


@pytest.mark.unit
@pytest.mark.analytics
class TestAnalyticsReportJsonSerializable:
    def test_revenue_metrics_are_json_serializable_floats(self, analytics_service, sample_order):
        start = datetime.now(UTC) - timedelta(days=1)
        end = datetime.now(UTC) + timedelta(days=1)

        metrics = analytics_service._get_revenue_metrics(start, end)

        # json.dumps mirrors the JSON-column write that crashed in prod.
        json.dumps(metrics)
        assert isinstance(metrics["total_revenue"], float)
        assert isinstance(metrics["average_order_value"], float)
        assert isinstance(metrics["previous_period_revenue"], float)
        assert isinstance(metrics["growth_rate"], float)

    def test_clv_analysis_average_is_json_serializable_float(self, analytics_service, sample_order):
        clv = analytics_service._get_customer_lifetime_value_analysis()

        json.dumps(clv)
        assert isinstance(clv["average_clv"], float)
