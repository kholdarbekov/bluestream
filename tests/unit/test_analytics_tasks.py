"""Regression tests for analytics report generation tasks.

The daily/weekly tasks must construct AnalyticsReport with the model's real
columns (report_type, title, start_date, end_date, report_data). They
previously passed period_start/period_end/data/generated_at, which raised
TypeError inside the task's try/except so the task returned {"error": ...}
and no report row was ever persisted.
"""

import inspect
import json
from unittest.mock import patch

import pytest

from business_app.models.analytics import AnalyticsReport
from business_app.models.sales import SalesAgentProfile
from business_app.models.user import User
from business_app.services.notification_service import NotificationService
from business_app.services.sales.agent_metrics_service import METRIC_KEYS, AgentMetricsService
from business_app.tasks import analytics_tasks
from business_app.utils.local_windows import previous_local_week
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserStatus, UserType
from tests.unit.test_sales_agent_role import make_sales_agent_user


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


def _install_notification_spy(monkeypatch):
    """Signature-enforcing spy over NotificationService.send_notification.

    The conftest autouse fixture already replaces this attribute with a mock
    whose signature *is* production's by contract (tests/conftest.py:325-331),
    so binding against it catches a payload/signature drift; unlike that mock,
    this one records what an admin would actually have been emailed.

    monkeypatch only (never a `patch()` context layered on top of the autouse
    monkeypatch of the SAME attribute) -- the restore order inverts and leaks
    the mock into the next test under `--dist=worksteal`.
    """
    signature = inspect.signature(NotificationService.send_notification)
    calls = []

    def _spy(self, *args, **kwargs):
        signature.bind(self, *args, **kwargs)  # TypeError on drift
        calls.append((args, kwargs))
        return {"mocked": {"success": True}}

    monkeypatch.setattr(NotificationService, "send_notification", _spy)
    return calls


def _manager(db):
    user = User(
        email="manager@example.com",
        phone="+998901234584",
        password_hash=hash_password("ManagerPassword123!"),
        first_name="Malika",
        last_name="Manager",
        user_type=UserType.STAFF,
        role=UserRole.MANAGER,
        status=UserStatus.ACTIVE,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _sales_agent(db, *, phone, first_name):
    """An ACTIVE sales agent: the row set `rows_for_period` publishes."""
    user = make_sales_agent_user(db, phone=phone, role=UserRole.SALES_AGENT, staff_roles=["sales_agent"])
    user.first_name = first_name
    db.session.add(SalesAgentProfile(user_id=user.id, is_active=True, districts=["chilanzar"]))
    db.session.commit()
    return user


@pytest.mark.unit
class TestGenerateWeeklyAgentPerformanceReport:
    """The weekly agent_performance email (plan Task 6 / ruling R14).

    Drives the real task against real seeded agents: the window helper
    (`previous_local_week`) and the row builder (`rows_for_period`) are the
    production ones, so a drift in either surfaces here rather than in an
    admin's inbox on a Monday morning.
    """

    def test_it_emails_every_admin_and_manager_one_row_per_active_agent(self, db, admin_user, monkeypatch):
        calls = _install_notification_spy(monkeypatch)
        manager = _manager(db)
        alisher = _sales_agent(db, phone="+998901234585", first_name="Alisher")
        zarina = _sales_agent(db, phone="+998901234586", first_name="Zarina")

        # The REAL helper, imported (never a local re-derivation of "last week").
        week_start, week_end = previous_local_week()

        result = analytics_tasks.generate_weekly_agent_performance_report.run()

        assert "error" not in result, f"task failed: {result}"
        assert result["success"] is True
        assert result["agent_count"] == 2
        assert result["week_start"] == week_start.isoformat()
        assert result["week_end"] == week_end.isoformat()

        # A whole previous LOCAL week, not "now minus 7 days".
        assert week_start.weekday() == 0
        assert week_end.weekday() == 6
        assert (week_end - week_start).days == 6

        # One email each, to admins and managers, and nobody else (the two
        # sales agents are users too, and must not be recipients).
        assert len(calls) == 2
        assert sorted(args[0] for args, _ in calls) == sorted([admin_user.id, manager.id])
        assert {args[1] for args, _ in calls} == {"agent_performance"}

        payload = calls[0][1]["template_data"]
        assert payload["week_start"] == week_start.isoformat()
        assert payload["week_end"] == week_end.isoformat()
        assert payload["agent_count"] == 2
        assert [row["agent_name"] for row in payload["agents"]] == [alisher.full_name, zarina.full_name]
        assert [row["agent_user_id"] for row in payload["agents"]] == [alisher.id, zarina.id]
        for row in payload["agents"]:
            assert set(METRIC_KEYS) <= set(row), f"row missing KPI keys: {sorted(set(METRIC_KEYS) - set(row))}"
        # Both agents sold nothing this week, so revenue ties at 0.0 and the
        # alphabetically first agent wins (rows are sorted by agent_name).
        assert payload["top_agent_name"] == alisher.full_name
        # Every recipient gets the SAME numbers.
        assert calls[1][1]["template_data"] == payload

        report = AnalyticsReport.query.get(result["report_id"])
        assert report is not None
        assert report.report_type == "agent_performance"
        assert report.title == (f"Agent Performance Report — week {week_start.isoformat()} to {week_end.isoformat()}")
        assert report.start_date is not None and report.end_date is not None
        assert report.start_date < report.end_date
        assert report.report_data["week_start"] == week_start.isoformat()
        assert report.report_data["agents"] == payload["agents"]
        # A Decimal leaking out of rows_for_period is the weekly-report
        # Decimal-JSON crash class: it would blow up on the JSON column commit.
        json.dumps(report.report_data)

    def test_the_top_agent_is_the_highest_revenue_row_and_nulls_travel_as_null(self, db, admin_user, monkeypatch):
        """`top_agent_name` is a revenue maximum, not the first or last row, and
        the rows reach the template verbatim (nulls included -- the templates,
        not the task, are what turn a null into an em dash)."""
        calls = _install_notification_spy(monkeypatch)

        rows = [
            {
                "agent_user_id": 41,
                "agent_name": "Alisher Agent",
                "phone": "+998901234585",
                **dict.fromkeys(METRIC_KEYS, 0),
                "revenue_delivered_paid": 1250000.0,
                "plan_vs_fact_pct": None,
            },
            {
                "agent_user_id": 42,
                "agent_name": "Zarina Agent",
                "phone": "+998901234586",
                **dict.fromkeys(METRIC_KEYS, 0),
                "revenue_delivered_paid": 4800000.0,
                "avg_visit_minutes": None,
            },
        ]
        real_signature = inspect.signature(AgentMetricsService.rows_for_period)
        captured = []

        def _rows_for_period(*args, **kwargs):
            real_signature.bind(*args, **kwargs)  # TypeError on drift
            captured.append((args, kwargs))
            return rows

        monkeypatch.setattr(AgentMetricsService, "rows_for_period", staticmethod(_rows_for_period))

        week_start, week_end = previous_local_week()
        result = analytics_tasks.generate_weekly_agent_performance_report.run()

        assert "error" not in result, f"task failed: {result}"
        # The task hands the window in positionally -- the previous local week.
        assert captured == [((week_start, week_end), {})]

        payload = calls[0][1]["template_data"]
        assert payload["agents"] == rows
        assert payload["agent_count"] == 2
        assert payload["top_agent_name"] == "Zarina Agent"
        assert payload["agents"][0]["plan_vs_fact_pct"] is None
        assert payload["agents"][1]["avg_visit_minutes"] is None

    def test_a_week_with_no_active_agents_sends_nothing_and_writes_no_report(self, db, admin_user, monkeypatch):
        """R25: SILENT when the roster is empty — no email, no report row.

        An estate with no sales agents is every estate before the role is rolled out and
        every estate after it is rolled back. A Monday email listing nobody is noise an
        admin learns to delete, and the habit costs them the week the table has rows in it.
        The daily managers' summary is silent at zero for the same reason (R9).
        """
        calls = _install_notification_spy(monkeypatch)
        before = AnalyticsReport.query.filter_by(report_type="agent_performance").count()

        result = analytics_tasks.generate_weekly_agent_performance_report.run()

        assert "error" not in result, f"task failed: {result}"
        assert result["agent_count"] == 0
        assert result["report_id"] is None
        assert calls == []
        assert AnalyticsReport.query.filter_by(report_type="agent_performance").count() == before

    def test_a_row_that_cannot_be_ranked_leaves_no_committed_report(self, db, admin_user, monkeypatch):
        """Nothing that can raise on a row may run AFTER the report is committed.

        Ranking the rows needs nothing the commit produces, so it belongs above it. The
        day `revenue_delivered_paid` goes nullable, the TypeError is swallowed by the
        task's own `except` -- and a commit placed first would leave an AnalyticsReport
        row that reads as a delivered report while nobody was ever emailed.
        """
        calls = _install_notification_spy(monkeypatch)
        rows = [
            {
                "agent_user_id": 41,
                "agent_name": "Alisher Agent",
                "phone": "+998901234585",
                **dict.fromkeys(METRIC_KEYS, 0),
                "revenue_delivered_paid": None,
            },
            {
                "agent_user_id": 42,
                "agent_name": "Zarina Agent",
                "phone": "+998901234586",
                **dict.fromkeys(METRIC_KEYS, 0),
                "revenue_delivered_paid": 1250000.0,
            },
        ]
        monkeypatch.setattr(AgentMetricsService, "rows_for_period", staticmethod(lambda *a, **k: rows))
        before = AnalyticsReport.query.filter_by(report_type="agent_performance").count()

        result = analytics_tasks.generate_weekly_agent_performance_report.run()

        assert "error" in result, f"an unrankable row must surface as an error: {result}"
        assert calls == [], "nobody may be emailed a report the task could not finish"
        assert AnalyticsReport.query.filter_by(report_type="agent_performance").count() == before
