"""Postgres-backed enforcement of the ``auditeventtype`` enum completeness.

This is the strongest possible guard for the prod incident: on a real,
fully-migrated Postgres database the ``audit_logs.event_type`` column is a
genuine ``auditeventtype`` enum, so inserting a row whose label is NOT in the
type raises ``psycopg2.errors.InvalidTextRepresentation`` (surfaced as
``sqlalchemy.exc.DataError``/``IntegrityError``) at flush/commit time — exactly
like production.

Regression: ``order_edited``, ``session_reopened`` and
``payment_verification_code_verified`` were added to the Python
``AuditEventType`` but never to the PG type, so audit inserts for those events
failed and the audit rows were silently dropped (``_log_to_database`` swallows
the error). Migration ``f3a9c7d21e84`` backfilled them.

The SQLite suite cannot catch this (Enum value not enforced), which is why
these tests run only against Postgres via the ``pg_app`` / ``pg_db`` fixtures.

Complements (does not duplicate)
tests/integration/test_migrations_roundtrip.py::test_auditeventtype_enum_covers_all_python_members,
which only checks the label SET. Here we additionally INSERT a real committed
row for EVERY member through the production ``audit_logger.log_event`` path and
prove a removed-label insert genuinely fails — proving end-to-end enforcement,
not just catalog presence.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError

from business_app.models.audit import AuditLog, AuditEventType, AuditSeverity


def _pg_enum_labels(db) -> set:
    return set(
        db.session.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON e.enumtypid = t.oid "
                "WHERE t.typname = 'auditeventtype'"
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.integration
class TestAuditEventTypeEnumPostgres:
    def test_pg_enum_labels_exactly_equal_python_values(self, pg_app, pg_db):
        """No drift in EITHER direction: PG label set == Python value set.

        ``missing`` (Python member with no PG label) is the exact prod bug.
        ``extra`` (PG label with no Python member) would mean a stale value the
        code can never produce — also worth flagging.
        """
        pg_labels = _pg_enum_labels(pg_db)
        python_values = {m.value for m in AuditEventType}

        missing = python_values - pg_labels
        extra = pg_labels - python_values

        assert not missing, f"PG auditeventtype missing labels: {sorted(missing)}"
        assert not extra, f"PG auditeventtype has stale labels: {sorted(extra)}"
        assert pg_labels == python_values

    def test_three_regressed_labels_are_present(self, pg_app, pg_db):
        """Pin the three specific labels the incident was about."""
        pg_labels = _pg_enum_labels(pg_db)
        for required in (
            "order_edited",
            "session_reopened",
            "payment_verification_code_verified",
        ):
            assert required in pg_labels, f"missing regressed label: {required}"

    @pytest.mark.parametrize(
        "member", list(AuditEventType), ids=lambda m: m.name
    )
    def test_log_event_commits_real_row_for_every_member(
        self, pg_app, pg_db, member
    ):
        """For EVERY member, ``log_event`` commits a real Postgres row.

        On Postgres a label missing from the enum would raise
        InvalidTextRepresentation here. This is the end-to-end guard that would
        have caught the three dropped labels.
        """
        from business_app.utils.audit_logger import audit_logger

        with pg_app.test_request_context("/x", method="POST"):
            event_id = audit_logger.log_event(event_type=member, action="probe")

        assert event_id is not None
        row = AuditLog.query.filter_by(event_id=event_id).first()
        assert row is not None, f"no row persisted for {member.name}"
        assert row.event_type is member

    @pytest.mark.parametrize(
        "value",
        ["order_edited", "session_reopened", "payment_verification_code_verified"],
    )
    def test_direct_insert_of_regressed_label_commits_on_pg(
        self, pg_app, pg_db, value
    ):
        """A direct AuditLog row with each regressed value commits cleanly.

        Before migration f3a9c7d21e84 this would have raised
        InvalidTextRepresentation on flush.
        """
        member = AuditEventType(value)
        row = AuditLog(
            event_id=str(uuid.uuid4()),
            event_type=member,
            severity=AuditSeverity.HIGH,
            action="probe-direct",
            success=True,
        )
        pg_db.session.add(row)
        pg_db.session.commit()

        reloaded = AuditLog.query.filter_by(event_id=row.event_id).one()
        assert reloaded.event_type is member

    def test_insert_with_label_not_in_enum_raises_on_pg(self, pg_app, pg_db):
        """A label absent from the PG enum MUST fail at the DB boundary.

        This proves the enum is genuinely enforced (so the prior parametrized
        all-members-commit test is a meaningful guard, not a no-op). We insert
        via raw SQL with a bogus label and expect a database error.
        """
        bogus = "definitely_not_a_real_event_type"
        with pytest.raises((DBAPIError, IntegrityError, StatementError)):
            pg_db.session.execute(
                text(
                    "INSERT INTO audit_logs "
                    "(event_id, event_type, severity, action, success, created_at) "
                    "VALUES (:eid, CAST(:et AS auditeventtype), "
                    "CAST(:sev AS auditseverity), :action, true, NOW())"
                ),
                {
                    "eid": str(uuid.uuid4()),
                    "et": bogus,
                    "sev": "high",
                    "action": "probe-bad",
                },
            )
            pg_db.session.flush()
        pg_db.session.rollback()

    def test_unlock_account_audit_member_is_a_valid_pg_label(self, pg_app, pg_db):
        """The exact member the unlock fix routes through is a real PG label.

        ``unlock_user_account`` logs ``USER_STATUS_CHANGED``; this asserts that
        inserting that member commits on Postgres (i.e. the fix's chosen member
        is genuinely usable, not just non-crashing in SQLite).
        """
        from business_app.utils.audit_logger import audit_logger

        with pg_app.test_request_context(
            "/api/v1/admin/users/unlock", method="POST"
        ):
            event_id = audit_logger.log_event(
                event_type=AuditEventType.USER_STATUS_CHANGED,
                action="unlock_account",
                severity=AuditSeverity.HIGH,
                resource_type="user",
                resource_id="123",
                additional_data={"admin_user_id": 999, "was_locked": True},
            )

        row = AuditLog.query.filter_by(event_id=event_id).one()
        assert row.event_type is AuditEventType.USER_STATUS_CHANGED
        assert row.action == "unlock_account"
        assert row.resource_id == "123"
        assert row.additional_data["admin_user_id"] == 999
