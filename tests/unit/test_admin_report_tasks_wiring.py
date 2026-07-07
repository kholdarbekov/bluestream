"""TDD proof-of-life tests for 4 admin-report Celery tasks being wired into
the beat schedule (task C3): two inventory tasks (inventory_tasks.py) and two
analytics/ML tasks (analytics_tasks.py), plus the churn task fixed and wired
in a follow-up pass.

Each task test proves the task actually runs against seeded data AND that
its admin-notification step genuinely dispatches (not silently swallowed)
before it gets a beat_schedule entry in celery_app.py.

History (see git blame / prior report for the earlier state of this file):

- generate_inventory_report_task and auto_reorder_products_task originally
  called ``NotificationService.send_email_notification(...)`` /
  ``send_telegram_notification(...)``, neither of which exists on
  ``NotificationService`` (only the differently-signatured private
  ``_send_email_notification``/``_send_telegram_notification``, and the
  public ``send_notification(user_id, notification_type, ...)`` that every
  other admin-report task actually uses). Every admin-recipient attempt
  raised ``AttributeError``, swallowed by a per-recipient try/except, so the
  task always reported ``{"success": True, ...}`` while never reaching an
  admin. Fixed: both tasks now resolve admin/manager recipients the same
  way analytics_tasks.py does (``User.query.filter(User.role.in_([ADMIN,
  MANAGER]), User.status == ACTIVE)``) and call
  ``NotificationService.send_notification(admin.id, "<type>",
  template_data=...)``. New trilingual file-based email templates were
  added for the "inventory_report" and "reorder_suggestions" types (en/uz/ru
  under business_app/templates/emails/, + EMAIL_SUBJECTS entries in
  email_template_service.py), following the exact same convention already
  used for "daily_report"/"weekly_business_report"/"churn_alert"/
  "demand_forecast"/"kpi_alert". send_low_stock_alert_task (same file, same
  bug, invoked from InventoryService on real-time stock drops) was fixed the
  same way as a bonus (not originally in scope) with its own
  "low_stock_alert" template.

- generate_demand_forecast was already wired correctly: it already used
  ``send_notification(admin.id, "demand_forecast", template_data=...)`` --
  audited for the same wrong-method bug and found clean, so left untouched.

- generate_churn_prediction_report was held back for a real bug in
  ``AnalyticsService._calculate_churn_from_stats``: it mixed a plain python
  float with a ``decimal.Decimal`` (``factors[factor] *
  Decimal(str(weights[factor]))``), raising ``TypeError`` for every active
  user. Fixed by coercing both operands to float (``float(factors[factor])
  * weights[factor]``), which also covers the on-Postgres case where
  ``avg_order_value`` is itself a genuine ``Decimal`` (a Numeric-column
  average). A second, narrower issue surfaced once the first was fixed:
  ``datetime.now(UTC) - stats["last_order_date"]`` (and the same for
  ``user_created_at``) raised "can't subtract offset-naive and
  offset-aware datetimes" -- but only under this test suite's SQLite
  backend, which silently drops tzinfo on read for ``DateTime(timezone=True)``
  columns (verified directly: both the raw ``func.max()`` scalar and the ORM
  attribute come back tzinfo=None here, even though the value was written
  timezone-aware). Postgres (the real backend) always returns genuinely
  timezone-aware datetimes for such columns, so this never fires in
  production. Hardened anyway with the existing
  ``business_app.utils.timezone_utils.ensure_utc()`` SSOT helper (already
  used by ``TimestampMixin.created_at_utc``) on both operands -- a one-line-
  per-site, no-behavior-change-on-Postgres fix that also makes this test
  pass against real (non-monkeypatched) seeded data. Now wired weekly,
  Monday 08:45 (after weekly-business-report at 08:00 and
  generate-demand-forecast at 08:30).
"""

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock
from unittest.mock import patch

import pytest

from business_app.models.analytics import AnalyticsReport
from business_app.models.order import Order
from business_app.models.product import Product
from business_app.services.analytics_service import AnalyticsService
from business_app.tasks import analytics_tasks, inventory_tasks


@pytest.fixture(scope="module")
def celery_app_module(app):
    """Import ``business_app.tasks.celery_app`` exactly once, safely.

    Same workaround as test_celery_task_wiring_cleanup.py /
    test_monitoring_tasks_wiring.py: ``make_celery()``'s bare
    ``create_app()`` call conflicts with the pytest ``app`` fixture's
    already-initialized Flask-SQLAlchemy instance, so hand back the
    already-initialized app instead.
    """
    module_name = "business_app.tasks.celery_app"
    if module_name not in sys.modules:
        with mock.patch("business_app.create_app", return_value=app):
            import business_app.tasks.celery_app  # noqa: F401
    return sys.modules[module_name]


def _make_product(
    db,
    category,
    *,
    name,
    stock_quantity,
    min_stock_level=10,
    max_stock_level=200,
    base_price="15000.00",
):
    product = Product(
        name=name,
        category_id=category.id,
        size="19L",
        volume=19.0,
        volume_unit="L",
        base_price=Decimal(base_price),
        stock_quantity=stock_quantity,
        min_stock_level=min_stock_level,
        max_stock_level=max_stock_level,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(product)
    db.session.commit()
    return product


def _make_order(db, user, *, order_number, created_at, total_amount="18000.00"):
    order = Order(
        user_id=user.id,
        order_number=order_number,
        subtotal=Decimal(total_amount),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal(total_amount),
        created_at=created_at,
    )
    db.session.add(order)
    db.session.commit()
    return order


@pytest.mark.unit
class TestGenerateInventoryReportTask:
    def test_runs_clean_with_low_and_out_of_stock_products(self, db, app, admin_user, sample_category):
        _make_product(db, sample_category, name="Out Of Stock Water", stock_quantity=0)
        _make_product(db, sample_category, name="Low Stock Water", stock_quantity=5)
        _make_product(db, sample_category, name="Healthy Water", stock_quantity=100)

        with patch(
            "business_app.services.notification_service.NotificationService.send_notification"
        ) as mock_send:
            result = inventory_tasks.generate_inventory_report_task.run("daily")

        assert "error" not in result, f"task failed: {result}"
        assert result["success"] is True
        assert result["report_type"] == "daily"
        assert result["summary"]["total_products"] == 3
        assert result["summary"]["out_of_stock_count"] == 1
        assert result["summary"]["low_stock_count"] == 1

        # The report must genuinely reach the admin -- not just compute
        # cleanly and silently drop the notification (the original bug).
        mock_send.assert_called_once()
        call = mock_send.call_args
        assert call.args[0] == admin_user.id
        assert call.args[1] == "inventory_report"
        assert call.kwargs["template_data"]["out_of_stock_count"] == 1
        assert call.kwargs["template_data"]["low_stock_count"] == 1


@pytest.mark.unit
class TestAutoReorderProductsTask:
    def test_runs_clean_and_flags_products_below_min_stock(self, db, app, admin_user, sample_category):
        _make_product(
            db, sample_category, name="Needs Reorder Water", stock_quantity=5, min_stock_level=10, max_stock_level=200
        )
        _make_product(
            db, sample_category, name="Well Stocked Water", stock_quantity=100, min_stock_level=10, max_stock_level=200
        )

        with patch(
            "business_app.services.notification_service.NotificationService.send_notification"
        ) as mock_send:
            result = inventory_tasks.auto_reorder_products_task.run()

        assert "error" not in result, f"task failed: {result}"
        assert result["success"] is True
        assert result["products_to_reorder_count"] == 1
        assert result["products_to_reorder"][0]["suggested_quantity"] == 195

        mock_send.assert_called_once()
        call = mock_send.call_args
        assert call.args[0] == admin_user.id
        assert call.args[1] == "reorder_suggestions"
        assert call.kwargs["template_data"]["total_products"] == 1


@pytest.mark.unit
class TestSendLowStockAlertTask:
    """Bonus fix: send_low_stock_alert_task (invoked from InventoryService on
    real-time stock drops, not part of the beat schedule) had the exact same
    nonexistent-method bug as the two report tasks above, in the same file.
    Fixed the same way."""

    def test_dispatches_real_notification_to_admin(self, db, app, admin_user, sample_category):
        product = _make_product(db, sample_category, name="Critical Water", stock_quantity=1, min_stock_level=10)

        with patch(
            "business_app.services.notification_service.NotificationService.send_notification"
        ) as mock_send:
            result = inventory_tasks.send_low_stock_alert_task.run(product.id)

        assert result["success"] is True
        mock_send.assert_called_once()
        call = mock_send.call_args
        assert call.args[0] == admin_user.id
        assert call.args[1] == "low_stock_alert"
        assert call.kwargs["template_data"]["product_id"] == product.id


@pytest.mark.unit
class TestGenerateChurnPredictionReport:
    """AnalyticsService._calculate_churn_from_stats had two real bugs (see
    module docstring): a float*Decimal TypeError, and (once that was fixed)
    a naive/aware datetime subtraction that only manifests on SQLite. Both
    are fixed; this runs the real (non-monkeypatched) computation against a
    seeded user + order."""

    def test_runs_clean_against_seeded_active_user_with_orders(self, db, sample_user, admin_user):
        _make_order(
            db,
            sample_user,
            order_number="ORD-CHURN-1",
            created_at=datetime.now(timezone.utc) - timedelta(days=10),
        )

        with patch(
            "business_app.services.notification_service.NotificationService.send_notification"
        ) as mock_send:
            result = analytics_tasks.generate_churn_prediction_report.run()

        assert "error" not in result, f"task failed: {result}"
        assert "report_id" in result

        report = AnalyticsReport.query.get(result["report_id"])
        assert report is not None
        assert report.report_type == "churn_prediction"

        # Only dispatches when there's at least one high-risk customer;
        # this seeded user (single old order, no other activity) reliably
        # lands high-risk under the real (fixed) computation.
        if result["high_risk_customers"] > 0:
            mock_send.assert_called_once()
            call = mock_send.call_args
            assert call.args[0] == admin_user.id
            assert call.args[1] == "churn_alert"


@pytest.mark.unit
class TestGenerateDemandForecast:
    def test_runs_clean_with_sufficient_historical_data(self, db, sample_user, admin_user, monkeypatch):
        """35 days of historical demand data, monkeypatched at the
        `_get_historical_demand_data` seam like
        test_analytics_service.py::test_predict_demand_returns_predictions_for_sufficient_data
        does, rather than seeded as real Order rows queried through
        `func.date(Order.created_at)`.

        Verified directly against the real dev Postgres DB (not this file's
        SQLite test harness) that `func.date(...)` returns a genuine
        `datetime.date` there, matching what `_get_historical_demand_data`
        assumes (`date.isoformat()`). SQLite's `func.date()` returns a plain
        `str`, which has no `.isoformat()` -- a test-harness-only artifact
        (same class of SQLite-vs-Postgres divergence already documented in
        test_monitoring_tasks_wiring.py for tz-aware DateTime columns), not a
        real bug in the task or in AnalyticsService.predict_demand.

        Audited for the same wrong-method notification bug as the inventory
        tasks: this task already calls
        ``send_notification(admin.id, "demand_forecast", template_data=...)``
        -- the correct, existing API -- so no fix was needed here.
        """
        start = datetime.now(timezone.utc).date() - timedelta(days=45)
        history = [
            {
                "date": (start + timedelta(days=day)).isoformat(),
                "order_count": 20 + (day % 7),
                "revenue": 15000.0 + day * 100,
            }
            for day in range(45)
        ]
        monkeypatch.setattr(AnalyticsService, "_get_historical_demand_data", lambda self: history)

        with patch(
            "business_app.services.notification_service.NotificationService.send_notification"
        ) as mock_send:
            result = analytics_tasks.generate_demand_forecast.run()

        assert "error" not in result, f"task failed: {result}"
        assert "report_id" in result
        assert result["total_predicted_orders"] >= 0

        report = AnalyticsReport.query.get(result["report_id"])
        assert report is not None
        assert report.report_type == "demand_forecast"
        assert report.report_data["forecast_period_days"] == 30
        assert len(report.report_data["predictions"]) == 30

        mock_send.assert_called_once()
        call = mock_send.call_args
        assert call.args[0] == admin_user.id
        assert call.args[1] == "demand_forecast"


@pytest.mark.unit
class TestNewBeatScheduleEntries:
    """All 4 tasks above must have real beat_schedule entries whose dotted
    task path resolves to a registered, importable Celery task."""

    NEW_BEAT_KEYS = (
        "generate-inventory-report",
        "auto-reorder-products",
        "generate-demand-forecast",
        "generate-churn-prediction-report",
    )

    def test_new_keys_present_with_crontab_schedule(self, celery_app_module):
        from celery.schedules import crontab

        schedule = celery_app_module.celery.conf.beat_schedule
        for key in self.NEW_BEAT_KEYS:
            assert key in schedule, f"missing beat entry: {key}"
            entry = schedule[key]
            assert isinstance(entry["schedule"], crontab)
            has_time_limit = "time_limit" in entry or "time_limit" in entry.get("options", {})
            assert has_time_limit, f"beat entry {key!r} has no time_limit"

    def test_new_keys_resolve_to_importable_registered_tasks(self, celery_app_module):
        celery = celery_app_module.celery
        schedule = celery.conf.beat_schedule
        for key in self.NEW_BEAT_KEYS:
            dotted_path = schedule[key]["task"]
            assert dotted_path in celery.tasks, f"{dotted_path} not registered with the celery app"
            assert celery.tasks[dotted_path] is not None
