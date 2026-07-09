"""TDD proof-of-life tests for the 5 monitoring/alert Celery tasks being wired
into the beat schedule (task C2), plus the two supporting fixes:

1. The `delivery_delay_alert` notification template (seeded via
   `seed_notification_templates()` in notification_service.py).
2. The driver-performance zero-stub in `AnalyticsService._get_driver_performance_metrics`,
   replaced with a real aggregation that reuses
   `DeliveryService.compute_driver_metrics` (also extracted out of
   `generate_driver_performance_report`).

Each task test proves the task actually runs against seeded data without
raising, before it gets a beat_schedule entry in celery_app.py.
"""

import sys
from datetime import datetime, timezone, timedelta
from unittest import mock
from unittest.mock import MagicMock

import pytest

from business_app.models.delivery import Delivery
from business_app.models.audit import AuditLog, AuditEventType, AuditSeverity
from business_app.services.analytics_service import AnalyticsService
from business_app.services.notification_service import (
    NotificationService,
    seed_notification_templates,
)
from business_app.tasks import analytics_tasks, order_tasks, delivery_tasks, audit_tasks
from business_app.utils.constants import NotificationChannel
from shared.enums import DeliveryStatus

NEW_BEAT_KEYS = (
    "monitor-business-kpis",
    "monitor-order-anomalies",
    "monitor-delivery-delays",
    "audit-log-statistics",
)


@pytest.fixture(scope="module")
def celery_app_module(app):
    """Import ``business_app.tasks.celery_app`` exactly once, safely.

    Its module-level ``celery = make_celery()`` calls the bare ``create_app()``
    (no config override), which conflicts with the pytest ``app`` fixture's
    already-initialized Flask-SQLAlchemy instance. Same workaround as
    test_celery_task_wiring_cleanup.py: monkeypatch ``business_app.create_app``
    for the single call ``make_celery()`` makes at import time.
    """
    module_name = "business_app.tasks.celery_app"
    if module_name not in sys.modules:
        with mock.patch("business_app.create_app", return_value=app):
            import business_app.tasks.celery_app  # noqa: F401
    return sys.modules[module_name]


def _make_order(db, user, *, order_number, total_amount="18000.00"):
    from business_app.models.order import Order
    from decimal import Decimal

    order = Order(
        user_id=user.id,
        order_number=order_number,
        subtotal=Decimal(total_amount),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal(total_amount),
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(order)
    db.session.commit()
    return order


@pytest.mark.unit
class TestMonitorBusinessKpis:
    def test_runs_clean_against_seeded_orders_and_deliveries(self, db, admin_user, sample_user, sample_order):
        """monitor_business_kpis must complete without raising and return the
        expected result shape (today/yesterday order+revenue comparison plus
        delivery-failure-rate check)."""
        delivery = Delivery(
            order_id=sample_order.id,
            status=DeliveryStatus.FAILED,
            scheduled_date=datetime.now(timezone.utc) - timedelta(hours=1),
            scheduled_time_slot="09:00-11:00",
        )
        db.session.add(delivery)
        db.session.commit()

        result = analytics_tasks.monitor_business_kpis.run()

        assert "error" not in result, f"task failed: {result}"
        assert "alerts_count" in result
        assert "today_orders" in result
        assert "today_revenue" in result


@pytest.mark.unit
class TestMonitorOrderAnomalies:
    def test_runs_clean_and_detects_empty_order(self, db, admin_user, sample_user, sample_order):
        """monitor_order_anomalies must complete without raising. sample_order
        has no order_items, so it should be flagged as a high-severity
        'empty_order' anomaly and trigger an (auto-mocked) admin alert."""
        # 4 orders in the last hour from the same user → excessive_orders (medium).
        for i in range(3):
            _make_order(db, sample_user, order_number=f"ORD-ANOM-{i}")

        result = order_tasks.monitor_order_anomalies.run()

        assert "error" not in result, f"task failed: {result}"
        assert result["total_anomalies"] >= 2  # excessive_orders + empty_order
        assert result["high_severity"] >= 1
        anomaly_types = {a["type"] for a in result["anomalies"]}
        assert "empty_order" in anomaly_types
        assert "excessive_orders" in anomaly_types


@pytest.mark.unit
class TestMonitorDeliveryDelays:
    def test_runs_clean_with_seeded_template_and_overdue_delivery(
        self, db, admin_user, sample_user, delivery_driver, sample_order
    ):
        """monitor_delivery_delays needs the delivery_delay_alert template to
        exist (seeded here) and an in-flight delivery overdue past the alert
        threshold (>12 hours) to alert on."""
        seed_notification_templates()

        # Must exceed the task's 12-hour alert threshold, not merely be overdue.
        overdue_eta = datetime.now(timezone.utc) - timedelta(hours=13)
        delivery = Delivery(
            order_id=sample_order.id,
            delivery_person_id=delivery_driver.id,
            status=DeliveryStatus.IN_TRANSIT,
            scheduled_date=overdue_eta - timedelta(hours=1),
            scheduled_time_slot="09:00-11:00",
            estimated_delivery_time=overdue_eta,
        )
        db.session.add(delivery)
        # flush (not commit): SQLite's DateTime(timezone=True) column loses
        # tzinfo on a real DB round-trip (Postgres preserves it in prod), so a
        # commit here would expire `delivery` and the task's re-query would
        # come back tz-naive, breaking `now - estimated_delivery_time` with a
        # TypeError unrelated to the task's real logic. flush() persists the
        # row for the task's query to find while keeping this aware Python
        # object live in the identity map.
        db.session.flush()

        result = delivery_tasks.monitor_delivery_delays.run()

        assert "error" not in result, f"task failed: {result}"
        assert result["overdue_deliveries"] == 1
        assert result["alerts_sent"] == 1

    def test_seeded_template_resolves_trilingually(self, db):
        """Prove the template itself (not just the globally-mocked
        send_notification in tests) is seeded and resolvable per language."""
        seed_notification_templates()

        service = NotificationService()
        for language in ("uz", "en", "ru"):
            template = service._get_notification_template(
                "delivery_delay_alert", NotificationChannel.EMAIL, language
            )
            assert template is not None, f"no delivery_delay_alert template for {language}"
            content = template.get_translated("content", language)
            subject = template.get_translated("subject", language)
            assert content and "{delay_minutes}" in content
            assert subject


@pytest.mark.unit
class TestGetAuditLogStatisticsTask:
    def test_runs_clean_against_seeded_audit_logs(self, db):
        for i in range(3):
            log = AuditLog(
                event_id=f"evt-{i}",
                event_type=AuditEventType.LOGIN_SUCCESS,
                severity=AuditSeverity.LOW,
                action="login",
            )
            db.session.add(log)
        db.session.commit()

        _run = audit_tasks.get_audit_log_statistics_task.run.__func__
        result = _run(MagicMock())

        assert result["status"] == "success"
        assert result["total_logs"] == 3


@pytest.mark.unit
class TestGenerateDriverPerformanceReport:
    def test_runs_clean_and_uses_shared_metrics_helper(self, db, delivery_driver, sample_order):
        now = datetime.now(timezone.utc)
        delivery = Delivery(
            order_id=sample_order.id,
            delivery_person_id=delivery_driver.id,
            status=DeliveryStatus.DELIVERED,
            scheduled_date=now - timedelta(hours=2),
            scheduled_time_slot="09:00-11:00",
            delivered_at=now,
            actual_delivery_time=now,
            distance_km=5.0,
            customer_rating=5,
            delivery_attempts=1,
        )
        db.session.add(delivery)
        db.session.commit()

        start = (now - timedelta(days=7)).isoformat()
        end = (now + timedelta(days=1)).isoformat()

        _run = delivery_tasks.generate_driver_performance_report.run.__func__
        result = _run(MagicMock(), delivery_driver.id, start, end)

        assert "error" not in result
        assert result["driver_id"] == delivery_driver.id
        assert result["metrics"]["total_deliveries"] == 1
        assert result["metrics"]["successful_deliveries"] == 1
        assert result["metrics"]["success_rate"] == 100.0
        assert result["metrics"]["average_rating"] == 5.0


@pytest.mark.unit
class TestDriverPerformanceMetricsStubReplacement:
    """Supporting fix #2: `_get_driver_performance_metrics` used to be a
    hardcoded-zero placeholder. It must now reflect real seeded deliveries,
    via the same `DeliveryService.compute_driver_metrics` helper used by
    `generate_driver_performance_report`."""

    def test_zero_when_no_deliveries_in_period(self, db):
        now = datetime.now(timezone.utc)
        result = AnalyticsService()._get_driver_performance_metrics(now - timedelta(days=7), now)
        assert result == {"total_drivers": 0, "average_deliveries_per_driver": 0, "top_performers": []}

    def test_non_zero_for_seeded_deliveries(self, db, delivery_driver, sample_order, sample_user):
        now = datetime.now(timezone.utc)

        # A second driver + order so we can assert aggregation across >1 driver.
        second_order = _make_order(db, sample_user, order_number="ORD-DRV2-001")
        from business_app.models.user import User
        from shared.enums import UserRole, UserType
        from business_app.utils.password_security import hash_password

        second_driver = User(
            email="driver2@example.com",
            phone="+998901234570",
            password_hash=hash_password("DriverPassword123!"),
            first_name="Second",
            last_name="Driver",
            user_type=UserType.STAFF,
            role=UserRole.DELIVERY_DRIVER,
            is_verified=True,
        )
        db.session.add(second_driver)
        db.session.commit()

        delivery_1 = Delivery(
            order_id=sample_order.id,
            delivery_person_id=delivery_driver.id,
            status=DeliveryStatus.DELIVERED,
            scheduled_date=now - timedelta(hours=2),
            scheduled_time_slot="09:00-11:00",
            delivered_at=now,
            actual_delivery_time=now,
            distance_km=5.0,
            customer_rating=5,
            delivery_attempts=1,
        )
        delivery_2 = Delivery(
            order_id=second_order.id,
            delivery_person_id=second_driver.id,
            status=DeliveryStatus.FAILED,
            scheduled_date=now - timedelta(hours=2),
            scheduled_time_slot="09:00-11:00",
            delivery_attempts=2,
        )
        db.session.add_all([delivery_1, delivery_2])
        db.session.commit()

        result = AnalyticsService()._get_driver_performance_metrics(now - timedelta(days=1), now + timedelta(days=1))

        assert result["total_drivers"] == 2
        assert result["average_deliveries_per_driver"] == 1.0
        assert len(result["top_performers"]) == 2
        top = result["top_performers"][0]
        assert top["driver_id"] == delivery_driver.id
        assert top["driver_name"] == delivery_driver.full_name
        assert top["successful_deliveries"] == 1
        assert top["total_deliveries"] == 1


@pytest.mark.unit
class TestNewBeatScheduleEntries:
    """The 4 tasks proven clean above must have real beat_schedule entries
    whose dotted task path resolves to a registered, importable Celery task.

    `generate_driver_performance_report` is intentionally NOT included: it
    requires a specific (driver_id, start_date, end_date) call, so it cannot
    be a static beat entry without a new fan-out orchestrator task, which is
    out of scope here (see task-C2-report.md).
    """

    def test_new_keys_present_with_crontab_schedule(self, celery_app_module):
        from celery.schedules import crontab

        schedule = celery_app_module.celery.conf.beat_schedule
        for key in NEW_BEAT_KEYS:
            assert key in schedule, f"missing beat entry: {key}"
            entry = schedule[key]
            assert isinstance(entry["schedule"], crontab)
            has_time_limit = "time_limit" in entry or "time_limit" in entry.get("options", {})
            assert has_time_limit, f"beat entry {key!r} has no time_limit"

    def test_new_keys_resolve_to_importable_registered_tasks(self, celery_app_module):
        celery = celery_app_module.celery
        schedule = celery.conf.beat_schedule
        for key in NEW_BEAT_KEYS:
            dotted_path = schedule[key]["task"]
            assert dotted_path in celery.tasks, f"{dotted_path} not registered with the celery app"
            assert celery.tasks[dotted_path] is not None

    def test_generate_driver_performance_report_not_wired_directly(self, celery_app_module):
        """Guard against accidentally beat-scheduling the per-driver task
        with no args (it would always no-op / error without a driver_id)."""
        schedule = celery_app_module.celery.conf.beat_schedule
        for entry in schedule.values():
            assert entry["task"] != "business_app.tasks.delivery_tasks.generate_driver_performance_report"
