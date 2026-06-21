"""Behavioural coverage for the audit logging path that silently dropped rows
in production.

These complement (do NOT duplicate):
  - tests/integration/test_migrations_roundtrip.py::test_auditeventtype_enum_covers_all_python_members
  - tests/unit/test_auth_service.py::test_unlock_user_account_audits_via_logger_with_valid_event_type

Why the old suite missed the bugs:
  - The audit ``event_type`` is a ``db.Enum`` whose *value* string is what gets
    sent to Postgres. On SQLite that Enum is NOT enforced by value, so an insert
    with a label missing from the PG type sailed through tests but blew up on
    prod with ``InvalidTextRepresentation`` — and the failure is swallowed by
    ``_log_to_database`` so no exception ever surfaced. The strongest guard for
    that (real enum enforcement) lives in the sibling PG file; here we exercise
    the full ``log_event`` -> ``_log_to_database`` -> persisted-row path and the
    ``unlock_user_account`` routing, asserting the ACTUAL persisted column
    values rather than mock kwargs.
"""

import pytest
from unittest.mock import patch

from business_app import db
from business_app.models.audit import AuditLog, AuditEventType, AuditSeverity
from business_app.utils.audit_logger import AuditLogger, audit_logger


def _all_logs():
    return AuditLog.query.order_by(AuditLog.id).all()


@pytest.mark.unit
@pytest.mark.auth
class TestAuditLoggerEventPersistence:
    """``audit_logger.log_event`` must actually write a complete, valid row."""

    def test_log_event_returns_event_id_and_persists_single_row(self, app, db):
        with app.test_request_context("/api/v1/anything", method="POST"):
            event_id = audit_logger.log_event(
                event_type=AuditEventType.USER_STATUS_CHANGED,
                action="unlock_account",
            )

        assert event_id is not None
        rows = _all_logs()
        assert len(rows) == 1
        row = rows[0]
        # event_id returned to caller must match the persisted row.
        assert row.event_id == event_id
        assert row.event_type == AuditEventType.USER_STATUS_CHANGED
        assert row.action == "unlock_account"

    def test_log_event_defaults_severity_medium_and_success_true(self, app, db):
        with app.test_request_context("/x", method="GET"):
            audit_logger.log_event(
                event_type=AuditEventType.LOGIN_SUCCESS,
                action="login",
            )

        row = _all_logs()[0]
        assert row.severity == AuditSeverity.MEDIUM
        assert row.success is True
        assert row.error_message is None

    def test_log_event_persists_all_explicit_fields(self, app, db):
        with app.test_request_context("/x", method="DELETE"):
            audit_logger.log_event(
                event_type=AuditEventType.ORDER_EDITED,
                action="edit_order",
                severity=AuditSeverity.HIGH,
                resource_type="order",
                resource_id=4321,
                description="Admin edited order 4321",
                old_values={"total": "100.00"},
                new_values={"total": "90.00"},
                success=False,
                error_message="validation failed",
                duration_ms=42,
                additional_data={"admin_user_id": 7},
            )

        row = _all_logs()[0]
        assert row.event_type == AuditEventType.ORDER_EDITED
        assert row.action == "edit_order"
        assert row.severity == AuditSeverity.HIGH
        assert row.resource_type == "order"
        # resource_id is coerced to str by log_event.
        assert row.resource_id == "4321"
        assert row.description == "Admin edited order 4321"
        assert row.old_values == {"total": "100.00"}
        assert row.new_values == {"total": "90.00"}
        assert row.success is False
        assert row.error_message == "validation failed"
        assert row.duration_ms == 42
        assert row.additional_data == {"admin_user_id": 7}

    def test_log_event_writes_via_independent_session_no_caller_commit(self, app, db):
        """``_log_to_database`` commits on its own dedicated session.

        Regression guard for the documented invariant: the audit write must be
        durable even though the *caller* never commits its own session. We add a
        throwaway object to the caller session, never commit it, then assert the
        audit row is persisted (visible via a fresh query) regardless.
        """
        with app.test_request_context("/x", method="POST"):
            event_id = audit_logger.log_event(
                event_type=AuditEventType.SESSION_REOPENED,
                action="reopen_session",
            )
            # Caller session is intentionally left dirty/uncommitted.
            db.session.rollback()

            row = AuditLog.query.filter_by(event_id=event_id).first()

        assert row is not None
        assert row.event_type == AuditEventType.SESSION_REOPENED

    def test_log_event_returns_none_and_writes_nothing_when_disabled(self, app, db):
        logger = AuditLogger()
        logger.enabled = False
        with app.test_request_context("/x", method="POST"):
            result = logger.log_event(
                event_type=AuditEventType.LOGIN_SUCCESS, action="login"
            )

        assert result is None
        assert _all_logs() == []

    def test_log_event_captures_request_context(self, app, db):
        with app.test_request_context(
            "/api/v1/admin/users/unlock",
            method="POST",
            headers={"User-Agent": "pytest-agent"},
        ):
            audit_logger.log_event(
                event_type=AuditEventType.USER_STATUS_CHANGED, action="unlock_account"
            )

        row = _all_logs()[0]
        assert row.method == "POST"
        assert row.user_agent == "pytest-agent"

    def test_log_event_works_without_request_context(self, app, db):
        """Audit must still persist when called outside a request (Celery/CLI)."""
        # app fixture already pushed an app context but NOT a request context.
        event_id = audit_logger.log_event(
            event_type=AuditEventType.SYSTEM_MAINTENANCE, action="maintenance"
        )

        assert event_id is not None
        row = AuditLog.query.filter_by(event_id=event_id).first()
        assert row is not None
        assert row.method is None  # no request -> no method captured
        assert row.endpoint is None


@pytest.mark.unit
@pytest.mark.auth
class TestAuditLoggerSwallowsFailures:
    """A failing audit write must never break the calling business operation."""

    def test_log_event_does_not_raise_when_db_insert_fails(self, app, db):
        """A failing DB write is swallowed inside ``_log_to_database`` so the
        caller's ``log_event`` still returns and never raises.

        We make the failure occur *inside* the try/except by breaking the
        AuditLog row construction — this mirrors prod, where an
        InvalidTextRepresentation on commit is caught there and not re-raised.
        """
        with app.test_request_context("/x", method="POST"):
            with patch(
                "business_app.utils.audit_logger.AuditLog",
                side_effect=Exception("db down"),
            ):
                # Must NOT raise even though the DB write blows up.
                event_id = audit_logger.log_event(
                    event_type=AuditEventType.PAYMENT_PROCESSED, action="pay"
                )

        # Still returns a generated event id to the caller; no row written.
        assert event_id is not None
        assert _all_logs() == []

    def test_internal_db_failure_is_caught_inside_log_to_database(self, app, db):
        """``_log_to_database`` itself swallows commit errors (logs, no raise)."""
        with app.test_request_context("/x", method="POST"):
            # Force the dedicated session commit to fail deep inside.
            with patch(
                "business_app.utils.audit_logger.AuditLog",
                side_effect=Exception("boom constructing row"),
            ):
                # Direct call to the private writer must not propagate.
                audit_logger._log_to_database(
                    {
                        "event_id": "x",
                        "event_type": AuditEventType.LOGIN_SUCCESS,
                        "severity": AuditSeverity.MEDIUM,
                        "action": "login",
                        "success": True,
                    }
                )
        # No row written, but no exception escaped.
        assert _all_logs() == []


@pytest.mark.unit
@pytest.mark.auth
class TestAuditLoggerSanitization:
    """Sensitive data must be scrubbed before it lands in the audit table."""

    def test_sensitive_keys_are_redacted_in_persisted_row(self, app, db):
        with app.test_request_context("/x", method="POST"):
            audit_logger.log_event(
                event_type=AuditEventType.PASSWORD_CHANGE,
                action="change_password",
                new_values={"password": "supersecret", "first_name": "Ann"},
            )

        row = _all_logs()[0]
        assert row.new_values["password"] == "[REDACTED]"
        assert row.new_values["first_name"] == "Ann"

    def test_long_strings_are_truncated_in_persisted_row(self, app, db):
        big = "z" * 2000
        with app.test_request_context("/x", method="POST"):
            audit_logger.log_event(
                event_type=AuditEventType.DATA_EXPORT,
                action="export",
                additional_data={"blob": big},
            )

        row = _all_logs()[0]
        assert row.additional_data["blob"].endswith("... [TRUNCATED]")
        assert len(row.additional_data["blob"]) < len(big)


@pytest.mark.unit
@pytest.mark.auth
class TestEveryEventTypePersists:
    """Exercise the insert path for every Python enum member.

    On SQLite the ``event_type`` Enum is not enforced by *value*, so this does
    not assert label validity (the PG sibling file does). It DOES prove that
    every member round-trips through ``log_event`` -> persisted row -> read-back
    with no exception and the correct member, which would catch a member whose
    Python ``.value`` is non-str / unhashable / breaks ``_log_to_file``.
    """

    @pytest.mark.parametrize("member", list(AuditEventType), ids=lambda m: m.name)
    def test_each_event_type_persists_and_reads_back(self, app, db, member):
        with app.test_request_context("/x", method="POST"):
            event_id = audit_logger.log_event(event_type=member, action="probe")

        row = AuditLog.query.filter_by(event_id=event_id).first()
        assert row is not None
        assert row.event_type is member
        # to_dict() reads .value of the enum — must not blow up.
        assert row.to_dict()["event_type"] == member.value


@pytest.mark.unit
@pytest.mark.auth
class TestUnlockUserAccountAudit:
    """Deeper coverage of ``AuthService.unlock_user_account`` audit routing.

    Prod bug: it referenced the nonexistent ``AuditEventType.ADMIN_ACTION`` plus
    an ``event_details`` kwarg that is not a column, so the audit silently failed
    and NO row was recorded. The existing single assertion only checks the mocked
    kwargs; here we assert the ACTUAL persisted row + that the operation is
    resilient to audit failures + the unlock side effects.
    """

    @pytest.fixture
    def auth_service(self, mock_redis):
        from business_app.services.auth_service import AuthService

        service = AuthService()
        service.redis_client = mock_redis
        return service

    def test_unlock_persists_audit_row_with_valid_member_and_metadata(
        self, auth_service, app, db, sample_user
    ):
        from datetime import datetime, timezone

        sample_user.account_locked_until = datetime.now(timezone.utc)
        sample_user.failed_login_attempts = 5
        db.session.commit()

        with app.test_request_context("/api/v1/admin/users/unlock", method="POST"):
            result = auth_service.unlock_user_account(
                sample_user.id, admin_user_id=999
            )

        assert result is True

        # A real audit row must exist (the prod bug recorded none).
        rows = AuditLog.query.filter_by(action="unlock_account").all()
        assert len(rows) == 1
        row = rows[0]
        # Valid member — NOT the nonexistent ADMIN_ACTION.
        assert row.event_type == AuditEventType.USER_STATUS_CHANGED
        assert row.event_type in set(AuditEventType)
        assert row.severity == AuditSeverity.HIGH
        # resource targeting captured.
        assert row.resource_type == "user"
        assert row.resource_id == str(sample_user.id)
        # additional_data carries the admin + prior-lock state.
        assert row.additional_data["admin_user_id"] == 999
        assert row.additional_data["was_locked"] is True

    def test_unlock_clears_lock_state_on_user(
        self, auth_service, app, db, sample_user
    ):
        from datetime import datetime, timezone

        sample_user.account_locked_until = datetime.now(timezone.utc)
        sample_user.failed_login_attempts = 3
        db.session.commit()

        with app.test_request_context("/x", method="POST"):
            auth_service.unlock_user_account(sample_user.id, admin_user_id=42)

        db.session.refresh(sample_user)
        assert sample_user.account_locked_until is None
        assert sample_user.failed_login_attempts == 0

    def test_unlock_records_was_locked_false_when_not_locked(
        self, auth_service, app, db, sample_user
    ):
        sample_user.account_locked_until = None
        sample_user.failed_login_attempts = 0
        db.session.commit()

        with app.test_request_context("/x", method="POST"):
            auth_service.unlock_user_account(sample_user.id, admin_user_id=1)

        row = AuditLog.query.filter_by(action="unlock_account").one()
        assert row.additional_data["was_locked"] is False

    def test_unlock_succeeds_even_if_audit_logging_raises(
        self, auth_service, app, db, sample_user
    ):
        """Audit failures must not fail the unlock (it is wrapped in try/except)."""
        with app.test_request_context("/x", method="POST"):
            with patch(
                "business_app.utils.audit_logger.audit_logger.log_event",
                side_effect=Exception("audit exploded"),
            ):
                result = auth_service.unlock_user_account(
                    sample_user.id, admin_user_id=5
                )

        assert result is True
        db.session.refresh(sample_user)
        assert sample_user.account_locked_until is None

    def test_unlock_missing_user_raises_not_found(
        self, auth_service, app, db
    ):
        from business_app.utils.exceptions import NotFoundError

        with app.test_request_context("/x", method="POST"):
            with pytest.raises(NotFoundError):
                auth_service.unlock_user_account(99999, admin_user_id=1)
