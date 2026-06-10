"""Regression tests for analytics report generation tasks.

The daily/weekly tasks must construct AnalyticsReport with the model's real
columns (report_type, title, start_date, end_date, report_data). They
previously passed period_start/period_end/data/generated_at, which raised
TypeError inside the task's try/except so the task returned {"error": ...}
and no report row was ever persisted.
"""

from unittest.mock import patch

import pytest

from business_app.models.analytics import AnalyticsReport
from business_app.tasks import analytics_tasks


def _report_payload():
    return {
        "overview": {
            "revenue": {"total_revenue": 150000.0, "growth_rate": 5.0},
            "orders": {"total_orders": 12, "completion_rate": 90.0},
            "customers": {"new_customers": 3, "repeat_rate": 30.0},
            "delivery": {"success_rate": 97.0, "average_delivery_time_hours": 2.0},
        }
    }


@pytest.mark.unit
class TestGenerateDailyAnalyticsReport:
    def test_persists_report_row_and_returns_success(self, db, admin_user):
        with (
            patch("business_app.tasks.analytics_tasks.AnalyticsService") as analytics_service_cls,
            patch("business_app.tasks.analytics_tasks.NotificationService") as notification_service_cls,
        ):
            analytics_service_cls.return_value.generate_business_report.return_value = _report_payload()

            result = analytics_tasks.generate_daily_analytics_report.run()

        assert "error" not in result, f"task failed: {result}"
        assert result["success"] is True

        report = AnalyticsReport.query.get(result["report_id"])
        assert report is not None
        assert report.report_type == "daily"
        assert report.title.startswith("Daily Analytics Report")
        assert report.start_date.date().isoformat() in report.title
        assert report.start_date is not None
        assert report.end_date is not None
        assert report.report_data == _report_payload()

        notification_service_cls.return_value.send_notification.assert_called_once()
        call = notification_service_cls.return_value.send_notification.call_args
        assert call.args[0] == admin_user.id
        assert call.args[1] == "daily_report"


@pytest.mark.unit
class TestGenerateWeeklyBusinessReport:
    def test_persists_report_row_and_returns_success(self, db, admin_user):
        with (
            patch("business_app.tasks.analytics_tasks.AnalyticsService") as analytics_service_cls,
            patch("business_app.tasks.analytics_tasks.NotificationService") as notification_service_cls,
        ):
            analytics_service_cls.return_value.generate_business_report.return_value = _report_payload()

            result = analytics_tasks.generate_weekly_business_report.run()

        assert "error" not in result, f"task failed: {result}"
        assert result["success"] is True

        report = AnalyticsReport.query.get(result["report_id"])
        assert report is not None
        assert report.report_type == "weekly"
        assert report.title.startswith("Weekly Business Report")
        assert report.end_date.date().isoformat() in report.title
        assert report.start_date is not None
        assert report.end_date is not None
        assert report.report_data == _report_payload()

        notification_service_cls.return_value.send_notification.assert_called_once()
        call = notification_service_cls.return_value.send_notification.call_args
        assert call.args[0] == admin_user.id
        assert call.args[1] == "weekly_business_report"
