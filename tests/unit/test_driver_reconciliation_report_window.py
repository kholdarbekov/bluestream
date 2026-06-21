"""Regression: COD driver-reconciliation report 'day' window is operator-tz aware.

Bug (deterministic CI failure in the post-midnight UTC window, real prod report
defect): DriverReconciliationService.get_report computed the period window in
Asia/Tashkent local dates but filtered sessions by their *UTC* date
(func.date(col)). A session collected in the early local morning (00:00-05:00
Tashkent = still the previous UTC day) was therefore dropped from "today's"
report. These tests pin boundary timestamps so they catch the bug at ANY
wall-clock time.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from business_app.models.payment import DriverCashSession
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from shared.enums import DriverCashSessionStatus


def _session(db, driver, *, started_utc, sid, status=DriverCashSessionStatus.MISMATCH, blocked=True):
    session = DriverCashSession(
        session_id=sid,
        driver_user_id=driver.id,
        status=status,
        session_started_at=started_utc,
        verified_at=started_utc,
        gross_cash_collected=Decimal("10000.00"),
        blocked_from_cod=blocked,
    )
    db.session.add(session)
    db.session.commit()
    return session


@pytest.mark.unit
class TestReconciliationReportWindowTimezone:
    def test_early_local_morning_session_appears_in_today(self, app, db, delivery_driver):
        # 2026-03-10 20:00 UTC == 2026-03-11 01:00 Asia/Tashkent (UTC+5):
        # the session belongs to the LOCAL day 2026-03-11.
        with app.app_context():
            _session(db, delivery_driver, started_utc=datetime(2026, 3, 10, 20, 0, tzinfo=UTC), sid="S-WIN-1")
            report = DriverReconciliationService().get_report(
                period="day",
                driver_user_id=delivery_driver.id,
                start_date=date(2026, 3, 11),
                end_date=date(2026, 3, 11),
            )
        assert report["summary"]["session_count"] == 1
        assert report["summary"]["mismatch_session_count"] == 1

    def test_previous_local_day_session_excluded(self, app, db, delivery_driver):
        # 2026-03-10 10:00 UTC == 2026-03-10 15:00 Tashkent -> belongs to 03-10,
        # must NOT leak into the 03-11 window (guards against over-inclusion).
        with app.app_context():
            _session(db, delivery_driver, started_utc=datetime(2026, 3, 10, 10, 0, tzinfo=UTC), sid="S-WIN-2")
            report = DriverReconciliationService().get_report(
                period="day",
                driver_user_id=delivery_driver.id,
                start_date=date(2026, 3, 11),
                end_date=date(2026, 3, 11),
            )
        assert report["summary"]["session_count"] == 0

    def test_late_local_evening_session_appears(self, app, db, delivery_driver):
        # 2026-03-11 18:00 UTC == 2026-03-11 23:00 Tashkent -> still LOCAL 03-11.
        with app.app_context():
            _session(db, delivery_driver, started_utc=datetime(2026, 3, 11, 18, 0, tzinfo=UTC), sid="S-WIN-3")
            report = DriverReconciliationService().get_report(
                period="day",
                driver_user_id=delivery_driver.id,
                start_date=date(2026, 3, 11),
                end_date=date(2026, 3, 11),
            )
        assert report["summary"]["session_count"] == 1

    def test_next_local_day_session_excluded(self, app, db, delivery_driver):
        # 2026-03-11 19:30 UTC == 2026-03-12 00:30 Tashkent -> belongs to 03-12.
        with app.app_context():
            _session(db, delivery_driver, started_utc=datetime(2026, 3, 11, 19, 30, tzinfo=UTC), sid="S-WIN-4")
            report = DriverReconciliationService().get_report(
                period="day",
                driver_user_id=delivery_driver.id,
                start_date=date(2026, 3, 11),
                end_date=date(2026, 3, 11),
            )
        assert report["summary"]["session_count"] == 0

    def test_period_day_with_now_is_time_of_day_robust(self, app, db, delivery_driver):
        # A session created 'now' must ALWAYS appear in the period='day' report
        # regardless of wall-clock — this is exactly what flaked at the boundary.
        with app.app_context():
            _session(db, delivery_driver, started_utc=datetime.now(UTC), sid="S-WIN-5")
            report = DriverReconciliationService().get_report(period="day", driver_user_id=delivery_driver.id)
        assert report["summary"]["session_count"] == 1
