"""Regression tests for audit logger log-level mapping.

Routine SUCCESS audit events must not escalate to ERROR/CRITICAL just
because of their severity classification — only actual failures keep
the severity-driven log level. Without this, e.g. a HIGH-severity
"inventory_confirmed_for_order ... SUCCESS" line lands at ERROR and
pollutes Grafana error dashboards.
"""

import logging

import pytest

from business_app.models.audit import AuditEventType, AuditSeverity
from business_app.utils.audit_logger import AuditLogger


@pytest.fixture
def file_only_audit_logger():
    """AuditLogger that only writes to the application logger (no DB)."""
    instance = AuditLogger()
    instance.log_to_database = False
    return instance


@pytest.fixture
def propagating_app_logger(app):
    """Re-enable propagation so caplog's root handler sees app.logger records."""
    original = app.logger.propagate
    app.logger.propagate = True
    yield app.logger
    app.logger.propagate = original


def _capture_audit_record(app, audit_logger_instance, caplog, *, severity, success):
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=app.logger.name):
        audit_logger_instance.log_event(
            event_type=AuditEventType.SYSTEM_MAINTENANCE,
            action="inventory_confirmed_for_order",
            severity=severity,
            success=success,
        )
    audit_records = [r for r in caplog.records if r.getMessage().startswith("AUDIT [")]
    assert len(audit_records) == 1, "expected exactly one audit log line"
    return audit_records[0]


class TestAuditLogLevelMapping:
    def test_high_severity_success_logs_at_info(
        self, app, file_only_audit_logger, propagating_app_logger, caplog
    ):
        record = _capture_audit_record(
            app, file_only_audit_logger, caplog, severity=AuditSeverity.HIGH, success=True
        )
        assert record.levelno == logging.INFO
        assert "SUCCESS" in record.getMessage()

    def test_high_severity_failure_logs_at_error(
        self, app, file_only_audit_logger, propagating_app_logger, caplog
    ):
        record = _capture_audit_record(
            app, file_only_audit_logger, caplog, severity=AuditSeverity.HIGH, success=False
        )
        assert record.levelno == logging.ERROR
        assert "FAILED" in record.getMessage()

    def test_critical_severity_success_logs_at_warning(
        self, app, file_only_audit_logger, propagating_app_logger, caplog
    ):
        record = _capture_audit_record(
            app, file_only_audit_logger, caplog, severity=AuditSeverity.CRITICAL, success=True
        )
        assert record.levelno == logging.WARNING

    def test_critical_severity_failure_logs_at_critical(
        self, app, file_only_audit_logger, propagating_app_logger, caplog
    ):
        record = _capture_audit_record(
            app, file_only_audit_logger, caplog, severity=AuditSeverity.CRITICAL, success=False
        )
        assert record.levelno == logging.CRITICAL

    def test_medium_severity_failure_logs_at_warning(
        self, app, file_only_audit_logger, propagating_app_logger, caplog
    ):
        record = _capture_audit_record(
            app, file_only_audit_logger, caplog, severity=AuditSeverity.MEDIUM, success=False
        )
        assert record.levelno == logging.WARNING

    def test_medium_severity_success_logs_at_info(
        self, app, file_only_audit_logger, propagating_app_logger, caplog
    ):
        record = _capture_audit_record(
            app, file_only_audit_logger, caplog, severity=AuditSeverity.MEDIUM, success=True
        )
        assert record.levelno == logging.INFO

    def test_low_severity_failure_logs_at_info(
        self, app, file_only_audit_logger, propagating_app_logger, caplog
    ):
        record = _capture_audit_record(
            app, file_only_audit_logger, caplog, severity=AuditSeverity.LOW, success=False
        )
        assert record.levelno == logging.INFO
