"""Unit tests for the INF-008 backup-freshness gauges.

These gauges (business_app/utils/prometheus_metrics.py) replace the ephemeral
celery-exporter task counter the backup alerts used to depend on. They are
derived on each /metrics scrape from the audit-log ground truth, so the tests
assert the derivation: newest completed/failed event → gauge value, best-effort
on missing rows, and TTL throttling.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from business_app.models.audit import AuditEventType, AuditLog, AuditSeverity
from business_app.utils import prometheus_metrics as pm


def _add_audit(db, action, created_at, *, success=True):
    """Insert one audit_logs row with an explicit created_at."""
    row = AuditLog(
        event_id=str(uuid.uuid4()),
        event_type=AuditEventType.SYSTEM_MAINTENANCE,
        severity=AuditSeverity.LOW if success else AuditSeverity.HIGH,
        action=action,
        success=success,
        created_at=created_at,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _gauge(g):
    return g._value.get()


@pytest.fixture(autouse=True)
def _reset_metric_state():
    """Isolate module-global gauge + TTL state between tests.

    Sentinel of -1 lets a test prove "left unchanged" (None result) distinctly
    from "set to 0".
    """
    pm.last_successful_db_backup_timestamp.set(-1)
    pm.last_successful_uploads_backup_timestamp.set(-1)
    pm.last_db_backup_failure_timestamp.set(-1)
    pm._backup_freshness_last_refresh = 0.0
    yield


@pytest.mark.unit
class TestBackupFreshnessGauges:
    def test_db_gauge_reflects_newest_completed_event(self, db):
        older = datetime(2026, 7, 10, 21, 30, tzinfo=timezone.utc)
        newer = datetime(2026, 7, 11, 21, 30, tzinfo=timezone.utc)
        _add_audit(db, "backup_database_completed", older)
        _add_audit(db, "backup_database_completed", newer)

        pm._refresh_backup_freshness_gauges()

        assert _gauge(pm.last_successful_db_backup_timestamp) == pytest.approx(newer.timestamp())

    def test_uploads_and_failure_gauges_set_independently(self, db):
        up = datetime(2026, 7, 11, 22, 0, tzinfo=timezone.utc)
        fail = datetime(2026, 7, 12, 21, 30, tzinfo=timezone.utc)
        _add_audit(db, "backup_uploads_completed", up)
        _add_audit(db, "backup_database_failed", fail, success=False)

        pm._refresh_backup_freshness_gauges()

        assert _gauge(pm.last_successful_uploads_backup_timestamp) == pytest.approx(up.timestamp())
        assert _gauge(pm.last_db_backup_failure_timestamp) == pytest.approx(fail.timestamp())

    def test_missing_events_leave_gauges_untouched(self, db):
        # No audit rows at all → None from the query → gauges must NOT be zeroed
        # (zeroing would false-fire DatabaseBackupStale). Sentinel -1 survives.
        pm._refresh_backup_freshness_gauges()

        assert _gauge(pm.last_successful_db_backup_timestamp) == -1
        assert _gauge(pm.last_successful_uploads_backup_timestamp) == -1
        assert _gauge(pm.last_db_backup_failure_timestamp) == -1

    def test_ttl_throttles_repeat_refreshes(self, db):
        t1 = datetime(2026, 7, 11, 21, 30, tzinfo=timezone.utc)
        _add_audit(db, "backup_database_completed", t1)
        pm._refresh_backup_freshness_gauges()
        assert _gauge(pm.last_successful_db_backup_timestamp) == pytest.approx(t1.timestamp())

        # A newer backup lands, but the TTL window has not elapsed → no re-query.
        t2 = datetime(2026, 7, 12, 21, 30, tzinfo=timezone.utc)
        _add_audit(db, "backup_database_completed", t2)
        pm._refresh_backup_freshness_gauges()
        assert _gauge(pm.last_successful_db_backup_timestamp) == pytest.approx(t1.timestamp())

        # Force the TTL to expire → picks up the newer backup.
        pm._backup_freshness_last_refresh = 0.0
        pm._refresh_backup_freshness_gauges()
        assert _gauge(pm.last_successful_db_backup_timestamp) == pytest.approx(t2.timestamp())

    def test_naive_created_at_treated_as_utc(self, db):
        # SQLite can hand back a naive datetime; the helper must assume UTC
        # rather than raise or apply local tz.
        naive = datetime(2026, 7, 11, 21, 30)  # no tzinfo
        _add_audit(db, "backup_database_completed", naive)

        pm._refresh_backup_freshness_gauges()

        expected = naive.replace(tzinfo=timezone.utc).timestamp()
        assert _gauge(pm.last_successful_db_backup_timestamp) == pytest.approx(expected)
