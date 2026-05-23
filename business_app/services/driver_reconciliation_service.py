"""Service for driver COD reconciliation sessions and reporting."""

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload

from business_app import db
from business_app.models.payment import CashCollectionEvent, DriverCashHandoff, DriverCashSession
from business_app.models.user import User
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from business_app.utils.constants import (
    NotificationChannel,
    NotificationType,
)
from shared.enums import (
    DriverCashSessionStatus,
    UserRole,
)
from business_app.utils.exceptions import ConflictError, NotFoundError, ValidationError


class DriverReconciliationService:
    """Driver cash reconciliation workflow service."""

    VERIFY_REASON_CODES = {
        "cash_count_matched",
        "cash_count_short",
        "cash_count_excess",
        "manual_override",
        "evidence_reviewed",
    }
    RESOLUTION_REASON_CODES = {
        "manager_approved_adjustment",
        "cash_recovered_later",
        "clerical_correction",
        "other",
    }
    RESOLUTION_ALLOWED_STATUSES = {
        DriverCashSessionStatus.MISMATCH.value,
        DriverCashSessionStatus.OVERDUE.value,
    }
    ACTIVE_STATUSES = {
        DriverCashSessionStatus.OPEN.value,
        DriverCashSessionStatus.PARTIAL.value,
        DriverCashSessionStatus.OVERDUE.value,
    }
    REOPEN_ALLOWED_STATUSES = {
        DriverCashSessionStatus.SUBMITTED.value,
        DriverCashSessionStatus.VERIFIED.value,
        DriverCashSessionStatus.MISMATCH.value,
        DriverCashSessionStatus.RESOLVED.value,
        DriverCashSessionStatus.OVERDUE.value,
    }

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        if value is None:
            return Decimal("0.00")
        return Decimal(str(value)).quantize(Decimal("0.01"))

    @staticmethod
    def _coerce_date(value: Optional[Any]) -> Optional[date]:
        if value is None:
            return None
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))

    @staticmethod
    def _normalize_period_window(period: str) -> Tuple[date, date]:
        tz = ZoneInfo(current_app.config.get("DISPLAY_TIMEZONE", "Asia/Tashkent"))
        now = datetime.now(tz)
        if period == "day":
            start_date = now.date()
        elif period == "week":
            start_date = (now - timedelta(days=6)).date()
        elif period == "month":
            start_date = (now - timedelta(days=29)).date()
        else:
            raise ValidationError("Invalid reconciliation report period")
        return start_date, now.date()

    @staticmethod
    def _status_value(status: Any) -> str:
        return status.value if hasattr(status, "value") else str(status or "")

    @staticmethod
    def _now_utc() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _as_aware_utc(value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _warning_days(self) -> int:
        days = int(current_app.config.get("COD_RECONCILIATION_WARNING_DAYS", 7) or 7)
        return max(days, 1)

    def _build_warning_due_at(self, session_started_at: Optional[datetime]) -> datetime:
        started_at = self._as_aware_utc(session_started_at) or self._now_utc()
        return started_at + timedelta(days=self._warning_days())

    def _session_has_cash_activity(self, session: DriverCashSession) -> bool:
        return self._to_decimal(session.gross_cash_collected) > Decimal("0.00")

    def _session_age_days(
        self,
        session: DriverCashSession,
        *,
        reference_time: Optional[datetime] = None,
    ) -> int:
        started_at = self._as_aware_utc(session.session_started_at or session.created_at)
        if not started_at:
            return 0
        reference = self._as_aware_utc(reference_time) or self._now_utc()
        return max(0, (reference - started_at).days)

    def _is_warning_due(
        self,
        session: DriverCashSession,
        *,
        reference_time: Optional[datetime] = None,
    ) -> bool:
        if not self._session_has_cash_activity(session):
            return False
        due_at = self._as_aware_utc(session.warning_due_at)
        if not due_at:
            due_at = self._build_warning_due_at(session.session_started_at)
            session.warning_due_at = due_at
        reference = self._as_aware_utc(reference_time) or self._now_utc()
        return reference >= due_at and not session.submitted_at

    def _build_risk_flags(self, session: DriverCashSession) -> List[str]:
        flags: List[str] = []
        warning_threshold = self._to_decimal(current_app.config.get("COD_CASH_WARNING_THRESHOLD_UZS", 200000))
        escalation_threshold = self._to_decimal(current_app.config.get("COD_CASH_ESCALATION_THRESHOLD_UZS", 400000))

        on_hand = self._to_decimal(session.expected_cash_on_hand)
        if on_hand >= escalation_threshold:
            flags.append("cash_on_hand_escalation")
        elif on_hand >= warning_threshold:
            flags.append("cash_on_hand_warning")

        rolling_window_start = self._now_utc() - timedelta(days=7)
        mismatch_count = (
            db.session.query(func.count(DriverCashSession.id))
            .filter(
                DriverCashSession.driver_user_id == session.driver_user_id,
                DriverCashSession.session_started_at >= rolling_window_start,
                DriverCashSession.status == DriverCashSessionStatus.MISMATCH,
            )
            .scalar()
            or 0
        )
        if mismatch_count >= 2:
            flags.append("repeated_mismatch_pattern")

        if self._status_value(session.status) == DriverCashSessionStatus.OVERDUE.value:
            flags.append("submission_overdue")

        if self._is_warning_due(session):
            flags.append("reconciliation_warning_due")

        return sorted(set(flags))

    def _build_next_actions(self, session: DriverCashSession) -> List[str]:
        status = self._status_value(session.status)
        actions: List[str] = []
        if status in {
            DriverCashSessionStatus.OPEN.value,
            DriverCashSessionStatus.PARTIAL.value,
            DriverCashSessionStatus.OVERDUE.value,
        }:
            actions.append("submit_reconciliation")
        if status == DriverCashSessionStatus.SUBMITTED.value:
            actions.append("await_admin_verification")
        if status in self.RESOLUTION_ALLOWED_STATUSES:
            actions.append("resolve_session")
        return actions

    def _serialize_session(
        self,
        session: DriverCashSession,
        *,
        include_events: bool = False,
        event_stats: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = session.to_dict()
        driver = session.driver_user
        events = list(session.cash_collection_events or []) if include_events else []
        stats = event_stats or {}
        expected_on_hand = self._to_decimal(session.expected_cash_on_hand)
        cumulative_declared = self._to_decimal(session.declared_cash)
        remaining = expected_on_hand - cumulative_declared
        if remaining < Decimal("0.00"):
            remaining = Decimal("0.00")
        payload.update(
            {
                "driver_name": driver.full_name if driver else None,
                "driver_phone": driver.phone if driver else None,
                "event_count": int(stats.get("event_count", len(events))),
                "delivery_count": int(stats.get("delivery_count", 0)),
                "total_cash_collected": float(session.gross_cash_collected or 0),
                "session_age_days": self._session_age_days(session),
                "is_warning_due": self._is_warning_due(session),
                "next_actions": self._build_next_actions(session),
                "cumulative_declared_cash": float(cumulative_declared),
                "remaining_cash_to_submit": float(remaining),
            }
        )
        if include_events and "delivery_count" not in stats:
            payload["delivery_count"] = len({event.delivery_id for event in events if event.delivery_id})
        if include_events:
            payload["events"] = [event.to_dict() for event in events]
            payload["handoffs"] = [
                handoff.to_dict() for handoff in (session.handoffs or []) if handoff.voided_at is None
            ]
            payload["voided_handoffs"] = [
                handoff.to_dict() for handoff in (session.handoffs or []) if handoff.voided_at is not None
            ]
        return payload

    def _assert_session_transition(self, session: DriverCashSession, *, operation: str) -> None:
        status = self._status_value(session.status)
        allowed = {
            "submit": {
                DriverCashSessionStatus.OPEN.value,
                DriverCashSessionStatus.PARTIAL.value,
                DriverCashSessionStatus.SUBMITTED.value,
                DriverCashSessionStatus.OVERDUE.value,
            },
            "verify": {
                DriverCashSessionStatus.OPEN.value,
                DriverCashSessionStatus.PARTIAL.value,
                DriverCashSessionStatus.SUBMITTED.value,
                DriverCashSessionStatus.OVERDUE.value,
                DriverCashSessionStatus.MISMATCH.value,
            },
            "resolve": self.RESOLUTION_ALLOWED_STATUSES,
        }.get(operation, set())
        if status not in allowed:
            raise ValidationError(f"Cannot {operation} session with current status '{status}'")

    def get_or_create_session(
        self,
        *,
        driver_user_id: int,
    ) -> DriverCashSession:
        driver = User.query.get(driver_user_id)
        if not driver:
            raise NotFoundError("Driver not found")

        session = (
            DriverCashSession.query.filter(
                DriverCashSession.driver_user_id == driver_user_id,
                DriverCashSession.status.in_(
                    [
                        DriverCashSessionStatus.OPEN,
                        DriverCashSessionStatus.PARTIAL,
                        DriverCashSessionStatus.OVERDUE,
                    ]
                ),
            )
            .order_by(DriverCashSession.session_started_at.desc(), DriverCashSession.id.desc())
            .first()
        )
        if not session:
            started_at = self._now_utc()
            warning_due_at = self._build_warning_due_at(started_at)
            session = DriverCashSession(
                driver_user_id=driver_user_id,
                status=DriverCashSessionStatus.OPEN,
                session_started_at=started_at,
                submission_due_at=warning_due_at,
                warning_due_at=warning_due_at,
                reminder_stage="none",
            )
            db.session.add(session)
            db.session.flush()

        self.refresh_expected_cash(session)
        return session

    def _sum_handoffs(self, session: DriverCashSession) -> Decimal:
        """Return the sum of unvoided handoff amounts for a session."""
        if session.id is None:
            return Decimal("0.00")
        total = (
            db.session.query(func.coalesce(func.sum(DriverCashHandoff.amount), 0))
            .filter(
                DriverCashHandoff.driver_cash_session_id == session.id,
                DriverCashHandoff.voided_at.is_(None),
            )
            .scalar()
        )
        return self._to_decimal(total)

    def refresh_expected_cash(self, session: DriverCashSession) -> DriverCashSession:
        gross_cash_collected = (
            db.session.query(func.coalesce(func.sum(CashCollectionEvent.amount), 0))
            .filter(
                CashCollectionEvent.driver_cash_session_id == session.id,
                CashCollectionEvent.voided_at.is_(None),
            )
            .scalar()
        )
        gross = self._to_decimal(gross_cash_collected)
        last_cash_activity_at = (
            db.session.query(func.max(CashCollectionEvent.occurred_at))
            .filter(
                CashCollectionEvent.driver_cash_session_id == session.id,
                CashCollectionEvent.voided_at.is_(None),
                CashCollectionEvent.amount > 0,
            )
            .scalar()
        )

        session.expected_cash = gross
        session.gross_cash_collected = gross
        session.expected_cash_on_hand = gross
        session.last_cash_activity_at = last_cash_activity_at
        session.warning_due_at = session.warning_due_at or self._build_warning_due_at(session.session_started_at)
        session.submission_due_at = session.submission_due_at or session.warning_due_at

        # declared_cash is the running sum of unvoided handoffs. We keep the
        # column on the session row for fast reads and admin reports; this
        # method is the single source of truth that keeps it in sync.
        handoff_total = self._sum_handoffs(session)
        session.declared_cash = handoff_total if handoff_total > Decimal("0.00") else None
        if session.declared_cash is not None:
            session.declared_variance = handoff_total - gross
        else:
            session.declared_variance = Decimal("0.00")
        if session.verified_cash is not None:
            session.verified_variance = self._to_decimal(session.verified_cash) - gross
        else:
            session.verified_variance = Decimal("0.00")

        session.risk_flags = self._build_risk_flags(session)
        return session

    def submit_session(
        self,
        *,
        driver_user_id: int,
        declared_cash: Any = None,
        notes: Optional[str] = None,
        submitted_by_user_id: Optional[int] = None,
    ) -> DriverCashSession:
        """Record a cash handoff toward closing the driver's active session.

        Each call inserts one ``DriverCashHandoff`` row. The session keeps
        ``status = PARTIAL`` (or ``OPEN`` until the first handoff) while the
        running total is below the expected on-hand amount; it transitions to
        ``SUBMITTED`` once the running total reaches or exceeds the expected
        amount. Over-submission closes the session with a positive declared
        variance and does **not** block the driver — the prior auto-MISMATCH
        on any non-zero driver variance was retired with the partial-handoff
        rollout because it masked legitimate over-payments and gave us no way
        to track legitimate under-payments separately.

        When ``declared_cash`` is ``None`` the call resolves to the remaining
        balance (``expected_cash_on_hand - already-handed-off``), so the
        "Submit expected cash" button always settles the remainder rather
        than re-stamping the full expected total.
        """
        session = self.get_or_create_session(driver_user_id=driver_user_id)
        self._assert_session_transition(session, operation="submit")

        now = self._now_utc()
        self.refresh_expected_cash(session)
        expected_on_hand = self._to_decimal(session.expected_cash_on_hand)
        prior_declared = self._to_decimal(session.declared_cash)

        if declared_cash is None:
            target_amount = expected_on_hand - prior_declared
        else:
            target_amount = self._to_decimal(declared_cash)

        if target_amount <= Decimal("0.00"):
            raise ValidationError("Handoff amount must be positive")

        recorder_id = submitted_by_user_id or driver_user_id
        handoff = DriverCashHandoff(
            driver_cash_session_id=session.id,
            amount=target_amount,
            occurred_at=now,
            recorded_by_user_id=recorder_id,
            notes=notes,
        )
        db.session.add(handoff)
        if notes:
            session.notes = notes
        db.session.flush()

        # Recompute declared_cash / variance from the now-current handoff set.
        self.refresh_expected_cash(session)
        cumulative_declared = self._to_decimal(session.declared_cash)

        next_session: Optional[DriverCashSession] = None
        closes_session = cumulative_declared >= expected_on_hand and expected_on_hand > Decimal("0.00")

        if closes_session:
            session.status = DriverCashSessionStatus.SUBMITTED
            session.submitted_at = now
            session.session_ended_at = now
            session.submitted_by_user_id = recorder_id
            session.blocked_from_cod = False
            session.block_reason = None
        else:
            # Partial handoff: keep the session active so the driver can
            # continue to settle the balance. The first partial moves OPEN/
            # OVERDUE into PARTIAL; subsequent partials stay PARTIAL.
            session.status = DriverCashSessionStatus.PARTIAL
            # Do not stamp submitted_at / session_ended_at / submitted_by:
            # those mark the close event, which has not happened yet.

        session.risk_flags = self._build_risk_flags(session)
        db.session.flush()

        if closes_session:
            next_session = self.get_or_create_session(driver_user_id=driver_user_id)
            session._next_active_session = next_session
        else:
            session._next_active_session = None

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action=("driver_cash_session_submitted" if closes_session else "driver_cash_handoff_recorded"),
            severity=AuditSeverity.MEDIUM,
            resource_type="driver_cash_session",
            resource_id=str(session.id),
            additional_data={
                "driver_user_id": driver_user_id,
                "session_started_at": (session.session_started_at.isoformat() if session.session_started_at else None),
                "handoff_id": handoff.id,
                "handoff_amount": float(target_amount),
                "expected_cash": float(session.expected_cash or 0),
                "expected_cash_on_hand": float(expected_on_hand),
                "cumulative_declared_cash": float(cumulative_declared),
                "declared_variance": float(session.declared_variance or 0),
                "status": self._status_value(session.status),
                "blocked_from_cod": session.blocked_from_cod,
                "next_driver_cash_session_id": next_session.id if next_session else None,
            },
        )

        db.session.commit()
        return session

    def verify_session(
        self,
        *,
        session_id: int,
        verified_cash: Any,
        actor_user_id: int,
        reason_code: str,
        notes: Optional[str] = None,
    ) -> DriverCashSession:
        session = DriverCashSession.query.options(
            joinedload(DriverCashSession.driver_user),
        ).get(session_id)
        if not session:
            raise NotFoundError("Driver cash session not found")
        self._assert_session_transition(session, operation="verify")
        if reason_code not in self.VERIFY_REASON_CODES:
            raise ValidationError("Invalid verify reason_code")

        self.refresh_expected_cash(session)
        session.verified_cash = self._to_decimal(verified_cash)
        session.verified_at = self._now_utc()
        session.verified_by_user_id = actor_user_id
        session.verification_notes = notes
        session.verification_reason_code = reason_code
        session.verified_variance = self._to_decimal(session.verified_cash) - self._to_decimal(
            session.expected_cash_on_hand
        )
        if session.verified_variance != Decimal("0.00") and not notes:
            raise ValidationError("Verification notes are required when verified cash differs from expected cash")

        if session.verified_variance == Decimal("0.00"):
            session.status = DriverCashSessionStatus.VERIFIED
            session.blocked_from_cod = False
            session.block_reason = None
        else:
            session.status = DriverCashSessionStatus.MISMATCH
            session.blocked_from_cod = True
            session.block_reason = "verified_cash_mismatch"

        session.risk_flags = self._build_risk_flags(session)

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action="driver_cash_session_verified",
            severity=AuditSeverity.MEDIUM if not session.blocked_from_cod else AuditSeverity.HIGH,
            resource_type="driver_cash_session",
            resource_id=str(session.id),
            additional_data={
                "driver_user_id": session.driver_user_id,
                "verified_by_user_id": actor_user_id,
                "verified_cash": float(session.verified_cash or 0),
                "expected_cash_on_hand": float(session.expected_cash_on_hand or 0),
                "verified_variance": float(session.verified_variance or 0),
                "blocked_from_cod": session.blocked_from_cod,
                "reason_code": reason_code,
            },
        )

        db.session.commit()
        return session

    def resolve_session(
        self,
        *,
        session_id: int,
        actor_user_id: int,
        reason_code: str,
        resolution_notes: str,
        verified_cash: Optional[Any] = None,
    ) -> DriverCashSession:
        session = DriverCashSession.query.get(session_id)
        if not session:
            raise NotFoundError("Driver cash session not found")
        self._assert_session_transition(session, operation="resolve")
        if not resolution_notes:
            raise ValidationError("Resolution notes are required")
        if reason_code not in self.RESOLUTION_REASON_CODES:
            raise ValidationError("Invalid resolve reason_code")

        self.refresh_expected_cash(session)
        if verified_cash is not None:
            session.verified_cash = self._to_decimal(verified_cash)
            session.verified_variance = self._to_decimal(session.verified_cash) - self._to_decimal(
                session.expected_cash_on_hand
            )
        session.verified_by_user_id = actor_user_id
        session.verified_at = self._now_utc()
        session.status = DriverCashSessionStatus.RESOLVED
        session.blocked_from_cod = False
        session.block_reason = None
        session.resolution_notes = resolution_notes
        session.resolution_reason_code = reason_code
        session.resolution_metadata = {
            "resolved_by_user_id": actor_user_id,
            "reason_code": reason_code,
        }
        session.risk_flags = self._build_risk_flags(session)

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action="driver_cash_session_resolved",
            severity=AuditSeverity.HIGH,
            resource_type="driver_cash_session",
            resource_id=str(session.id),
            additional_data={
                "driver_user_id": session.driver_user_id,
                "resolved_by_user_id": actor_user_id,
                "resolution_notes": resolution_notes,
                "reason_code": reason_code,
            },
        )

        db.session.commit()
        return session

    def reopen_session(
        self,
        *,
        session_id: int,
        actor_user_id: int,
        reason: str,
        commit: bool = True,
    ) -> DriverCashSession:
        """Reopen a closed cash session so order-edit cascades can re-tally.

        Used when an admin retroactively edits a delivered order whose cash
        session has already been submitted/verified/resolved. The session
        returns to OPEN; the driver re-submits and an admin re-verifies via
        the usual submit/verify flow. Verification fields are cleared so the
        re-verification starts from a clean slate.
        """
        if not reason or not reason.strip():
            raise ValidationError("A reason is required to reopen a cash session")

        session = DriverCashSession.query.get(session_id)
        if not session:
            raise NotFoundError("Driver cash session not found")

        status = self._status_value(session.status)
        if status not in self.REOPEN_ALLOWED_STATUSES:
            raise ValidationError(
                f"Cannot reopen session with current status '{status}'",
            )

        # Partial unique index uq_driver_cash_sessions_driver_active permits at
        # most one session per driver in {open, overdue}. If a newer one exists,
        # the admin must first resolve it (verify/resolve) so re-opening this
        # older session does not collide.
        active_conflict = (
            DriverCashSession.query.filter(
                DriverCashSession.driver_user_id == session.driver_user_id,
                DriverCashSession.id != session.id,
                DriverCashSession.status.in_([DriverCashSessionStatus.OPEN, DriverCashSessionStatus.OVERDUE]),
            )
            .order_by(DriverCashSession.session_started_at.desc())
            .first()
        )
        if active_conflict:
            raise ConflictError(
                f"Cannot reopen session {session.id}: driver has another active "
                f"session (id={active_conflict.id}, status={self._status_value(active_conflict.status)}). "
                "Submit and verify the active session first.",
                error_code="CASH_SESSION_ACTIVE_CONFLICT",
            )

        now = self._now_utc()
        session.status = DriverCashSessionStatus.OPEN
        session.reopened_at = now
        session.reopened_by_user_id = actor_user_id
        session.reopened_reason = reason.strip()
        session.reopen_count = (session.reopen_count or 0) + 1
        # Clear verification trail so the re-verification cycle is unambiguous.
        session.submitted_at = None
        session.submitted_by_user_id = None
        session.verified_at = None
        session.verified_by_user_id = None
        session.verification_notes = None
        session.verification_reason_code = None
        # Unblock driver: the prior block was tied to the now-stale variance.
        if session.blocked_from_cod:
            session.blocked_from_cod = False
            session.block_reason = None
        # session_ended_at must clear too, otherwise the session looks closed
        # to listings that filter on it.
        session.session_ended_at = None

        # Void all prior handoffs so the driver re-submits from scratch.
        # We preserve the rows (not delete) for audit; the running sum that
        # backs declared_cash filters out voided handoffs.
        for prior_handoff in session.handoffs:
            if prior_handoff.voided_at is None:
                prior_handoff.voided_at = now
                prior_handoff.voided_by_user_id = actor_user_id
                prior_handoff.void_reason = "session_reopened"

        self.refresh_expected_cash(session)

        audit_logger.log_event(
            event_type=AuditEventType.SESSION_REOPENED,
            action="driver_cash_session_reopened",
            severity=AuditSeverity.HIGH,
            resource_type="driver_cash_session",
            resource_id=str(session.id),
            additional_data={
                "driver_user_id": session.driver_user_id,
                "actor_user_id": actor_user_id,
                "previous_status": status,
                "reopen_count": session.reopen_count,
                "reason": session.reopened_reason,
            },
        )

        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return session

    def mark_overdue_sessions(
        self,
        *,
        reference_time: Optional[datetime] = None,
    ) -> int:
        reference_time = self._as_aware_utc(reference_time) or self._now_utc()
        updated = 0
        sessions = DriverCashSession.query.filter(
            DriverCashSession.status == DriverCashSessionStatus.OPEN,
        ).all()

        for session in sessions:
            self.refresh_expected_cash(session)
            if session.submitted_at:
                continue
            if self._is_warning_due(session, reference_time=reference_time):
                session.status = DriverCashSessionStatus.OVERDUE
                session.blocked_from_cod = False
                if session.block_reason == "reconciliation_overdue":
                    session.block_reason = None
                session.risk_flags = self._build_risk_flags(session)
                updated += 1

        if updated:
            db.session.commit()
        return updated

    def get_sessions_due_for_reminder(
        self,
        *,
        reference_time: Optional[datetime] = None,
    ) -> List[Tuple[DriverCashSession, str]]:
        reference_time = self._as_aware_utc(reference_time) or self._now_utc()
        interval_minutes = int(current_app.config.get("COD_REMINDER_INTERVAL_MINUTES", 60))
        sessions = (
            DriverCashSession.query.options(
                joinedload(DriverCashSession.driver_user),
            )
            .filter(
                DriverCashSession.status.in_(
                    [
                        DriverCashSessionStatus.OPEN,
                        DriverCashSessionStatus.OVERDUE,
                    ]
                ),
            )
            .all()
        )

        due_sessions: List[Tuple[DriverCashSession, str]] = []
        for session in sessions:
            self.refresh_expected_cash(session)
            if session.submitted_at:
                continue

            if session.last_reminder_at:
                elapsed = reference_time - self._as_aware_utc(session.last_reminder_at)
                if elapsed < timedelta(minutes=interval_minutes):
                    continue

            if self._is_warning_due(session, reference_time=reference_time):
                if self._status_value(session.status) == DriverCashSessionStatus.OPEN.value:
                    session.status = DriverCashSessionStatus.OVERDUE
                    session.blocked_from_cod = False
                    if session.block_reason == "reconciliation_overdue":
                        session.block_reason = None
                    session.risk_flags = self._build_risk_flags(session)
                due_sessions.append((session, "overdue"))

        return due_sessions

    def send_due_reconciliation_reminders(
        self,
        *,
        reference_time: Optional[datetime] = None,
    ) -> Dict[str, int]:
        reference_time = self._as_aware_utc(reference_time) or self._now_utc()
        pending = self.get_sessions_due_for_reminder(reference_time=reference_time)
        sent = 0
        failed = 0
        for session, stage in pending:
            try:
                self._send_driver_reconciliation_reminder(session, stage=stage)
                session.last_reminder_at = reference_time
                session.reminder_stage = stage
                sent += 1
            except Exception:
                failed += 1

        if sent:
            db.session.commit()

        return {"sent": sent, "failed": failed}

    def _send_driver_reconciliation_reminder(self, session: DriverCashSession, *, stage: str) -> None:
        driver = session.driver_user or User.query.get(session.driver_user_id)
        if not driver:
            return

        from business_app.services.notification_service import NotificationService
        from business_app.utils.translations import get_translation

        # B-1: localize backend-emitted staff Telegram notifications.
        # The driver's preferred_language is the source of truth — falling
        # back to 'en' only when the column is null. Translation keys live
        # in scripts/seed_staff_translations.py and are seeded under the
        # 'staff_bot' category, so the same DB row backs both the staff bot
        # i18n.get(...) calls and these backend-side lookups.
        driver_language = getattr(driver, "preferred_language", None) or "en"
        body_key = (
            "staff.notification.reconciliation_reminder_overdue"
            if stage == "overdue"
            else "staff.notification.reconciliation_reminder_due"
        )
        body = get_translation(
            body_key,
            language=driver_language,
            date=session.session_started_at.date().isoformat(),
            expected_cash=f"{float(session.expected_cash_on_hand or 0):,.0f}",
        )
        subject = get_translation(
            "staff.notification.subject.driver_cash_reconciliation",
            language=driver_language,
        )

        # The legacy `template_override.get_translated` lambda ignored its
        # `language` argument and always returned the pre-built string. We
        # now resolve per language so the in-app and Telegram surfaces both
        # honour the recipient's preference.
        def _get_translated(field_name, lang):
            target_lang = lang or driver_language
            if field_name == "subject":
                return get_translation(
                    "staff.notification.subject.driver_cash_reconciliation",
                    language=target_lang,
                )
            return get_translation(
                body_key,
                language=target_lang,
                date=session.session_started_at.date().isoformat(),
                expected_cash=f"{float(session.expected_cash_on_hand or 0):,.0f}",
            )

        template = SimpleNamespace(
            subject=subject,
            content=body,
            get_translated=_get_translated,
        )

        notification_service = NotificationService()
        notification_service.send_notification(
            user_id=driver.id,
            notification_type=NotificationType.SYSTEM_ALERT,
            channels=[NotificationChannel.IN_APP],
            template_data={
                "session_started_at": (session.session_started_at.isoformat() if session.session_started_at else None),
                "expected_cash_on_hand": float(session.expected_cash_on_hand or 0),
                "reminder_stage": stage,
            },
            template_override=template,
        )

        if getattr(driver, "telegram_id", None):
            # Pass `language` explicitly so the Telegram path resolves in the
            # driver's language even if NotificationService falls back to
            # `user.preferred_language` lookup downstream.
            notification_service.send_staff_telegram_message(driver, body, language=driver_language)

    def notify_managers_about_exception_sessions(self) -> int:
        high_risk_sessions = DriverCashSession.query.filter(
            DriverCashSession.status.in_(
                [
                    DriverCashSessionStatus.OVERDUE,
                    DriverCashSessionStatus.MISMATCH,
                ]
            ),
        ).count()
        if not high_risk_sessions:
            return 0

        managers = User.query.filter(
            User.role.in_([UserRole.ADMIN, UserRole.MANAGER]),
            User.status == "active",
        ).all()
        if not managers:
            return 0

        from business_app.services.notification_service import NotificationService
        from business_app.utils.translations import get_translation

        # B-1: each manager may speak a different language, so we can't
        # build the body once outside the loop the way the legacy code did.
        # The translation lookup is Redis-cached, so per-manager calls are
        # effectively free after the first warm read.
        notified = 0
        for manager in managers:
            try:
                manager_language = getattr(manager, "preferred_language", None) or "en"
                body = get_translation(
                    "staff.notification.manager_exception_summary",
                    language=manager_language,
                    count=high_risk_sessions,
                )

                def _get_translated(field_name, lang, _count=high_risk_sessions):
                    target_lang = lang or manager_language
                    if field_name == "subject":
                        return get_translation(
                            "staff.notification.subject.driver_cash_exceptions",
                            language=target_lang,
                        )
                    return get_translation(
                        "staff.notification.manager_exception_summary",
                        language=target_lang,
                        count=_count,
                    )

                template = SimpleNamespace(
                    subject=get_translation(
                        "staff.notification.subject.driver_cash_exceptions",
                        language=manager_language,
                    ),
                    content=body,
                    get_translated=_get_translated,
                )
                NotificationService().send_notification(
                    user_id=manager.id,
                    notification_type=NotificationType.SYSTEM_ALERT,
                    channels=[NotificationChannel.IN_APP],
                    template_data={
                        "high_risk_session_count": high_risk_sessions,
                    },
                    template_override=template,
                )
                notified += 1
            except Exception:
                continue
        return notified

    def is_driver_blocked_from_cod(self, driver_user_id: int) -> bool:
        blocked_session = DriverCashSession.query.filter(
            DriverCashSession.driver_user_id == driver_user_id,
            DriverCashSession.blocked_from_cod.is_(True),
        ).first()
        return blocked_session is not None

    def get_open_session_for_driver(self, driver_user_id: int) -> DriverCashSession:
        return self.get_or_create_session(driver_user_id=driver_user_id)

    def get_session_detail(self, session_id: int) -> Dict[str, Any]:
        session = DriverCashSession.query.options(
            joinedload(DriverCashSession.driver_user),
            joinedload(DriverCashSession.cash_collection_events),
        ).get(session_id)
        if not session:
            raise NotFoundError("Driver cash session not found")
        self.refresh_expected_cash(session)
        return self._serialize_session(session, include_events=True)

    def _event_stats_for_sessions(self, session_ids: List[int]) -> Dict[int, Dict[str, int]]:
        if not session_ids:
            return {}
        rows = (
            db.session.query(
                CashCollectionEvent.driver_cash_session_id,
                func.count(CashCollectionEvent.id),
                func.count(func.distinct(CashCollectionEvent.delivery_id)),
            )
            .filter(
                CashCollectionEvent.driver_cash_session_id.in_(session_ids),
                CashCollectionEvent.voided_at.is_(None),
            )
            .group_by(CashCollectionEvent.driver_cash_session_id)
            .all()
        )
        return {
            session_id: {
                "event_count": int(event_count or 0),
                "delivery_count": int(delivery_count or 0),
            }
            for session_id, event_count, delivery_count in rows
        }

    def _apply_session_window_filters(self, query, *, start_date: Optional[Any], end_date: Optional[Any]):
        # Only genuinely active (open/partial/overdue) sessions count as "ongoing"
        # and stay visible in every window. Closed sessions use their real end
        # date — note session_ended_at is NULL for sessions verified/resolved
        # without a driver submission, so fall back through other close timestamps.
        is_active = DriverCashSession.status.in_(
            [
                DriverCashSessionStatus.OPEN,
                DriverCashSessionStatus.PARTIAL,
                DriverCashSessionStatus.OVERDUE,
            ]
        )
        effective_end = func.coalesce(
            DriverCashSession.session_ended_at,
            DriverCashSession.submitted_at,
            DriverCashSession.verified_at,
            DriverCashSession.last_cash_activity_at,
            DriverCashSession.updated_at,
        )
        if start_date:
            normalized_start = self._coerce_date(start_date)
            query = query.filter(
                or_(
                    is_active,
                    func.date(effective_end) >= normalized_start,
                )
            )
        if end_date:
            normalized_end = self._coerce_date(end_date)
            query = query.filter(func.date(DriverCashSession.session_started_at) <= normalized_end)
        return query

    def _apply_warning_only_filter(self, query):
        now = self._now_utc()
        cutoff_started_at = now - timedelta(days=self._warning_days())
        return query.filter(
            DriverCashSession.status.in_(
                [
                    DriverCashSessionStatus.OPEN,
                    DriverCashSessionStatus.PARTIAL,
                    DriverCashSessionStatus.OVERDUE,
                ]
            ),
            DriverCashSession.blocked_from_cod.is_(False),
            DriverCashSession.submitted_at.is_(None),
            DriverCashSession.gross_cash_collected > 0,
            or_(
                DriverCashSession.warning_due_at <= now,
                DriverCashSession.warning_due_at.is_(None)
                & (DriverCashSession.session_started_at <= cutoff_started_at),
            ),
        )

    def list_sessions(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        status: Optional[str] = None,
        driver_user_id: Optional[int] = None,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        blocked_only: bool = False,
        warning_only: bool = False,
        min_session_age_days: Optional[int] = None,
    ) -> Dict[str, Any]:
        query = DriverCashSession.query.options(
            joinedload(DriverCashSession.driver_user),
        )

        if status:
            try:
                status_enum = DriverCashSessionStatus(status)
            except ValueError as exc:
                raise ValidationError("Invalid driver cash session status") from exc
            query = query.filter(DriverCashSession.status == status_enum)
        if driver_user_id:
            query = query.filter(DriverCashSession.driver_user_id == driver_user_id)
        query = self._apply_session_window_filters(query, start_date=start_date, end_date=end_date)
        if blocked_only:
            query = query.filter(DriverCashSession.blocked_from_cod.is_(True))
        if warning_only:
            query = self._apply_warning_only_filter(query)
        if min_session_age_days is not None:
            try:
                min_days = max(0, int(min_session_age_days))
            except (TypeError, ValueError) as exc:
                raise ValidationError("min_session_age_days must be an integer") from exc
            cutoff = self._now_utc() - timedelta(days=min_days)
            query = query.filter(DriverCashSession.session_started_at <= cutoff)

        pagination = query.order_by(
            DriverCashSession.session_started_at.desc(),
            DriverCashSession.id.desc(),
        ).paginate(page=page, per_page=min(per_page, 100), error_out=False)

        session_ids = [session.id for session in pagination.items]
        event_stats = self._event_stats_for_sessions(session_ids)
        items = []
        for session in pagination.items:
            items.append(
                self._serialize_session(
                    session,
                    event_stats=event_stats.get(session.id),
                )
            )

        return {
            "items": items,
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
        }

    def get_report(
        self,
        *,
        period: str = "day",
        driver_user_id: Optional[int] = None,
        page: int = 1,
        per_page: int = 20,
        status: Optional[str] = None,
        blocked_only: bool = False,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        warning_only: bool = False,
        min_session_age_days: Optional[int] = None,
    ) -> Dict[str, Any]:
        default_start_date, default_end_date = self._normalize_period_window(period)
        start_date = self._coerce_date(start_date) or default_start_date
        end_date = self._coerce_date(end_date) or default_end_date
        sessions_result = self.list_sessions(
            page=page,
            per_page=per_page,
            status=status,
            driver_user_id=driver_user_id,
            start_date=start_date,
            end_date=end_date,
            blocked_only=blocked_only,
            warning_only=warning_only,
            min_session_age_days=min_session_age_days,
        )

        report_query = DriverCashSession.query.options(
            joinedload(DriverCashSession.driver_user),
        )
        report_query = self._apply_session_window_filters(report_query, start_date=start_date, end_date=end_date)
        if status:
            try:
                status_enum = DriverCashSessionStatus(status)
            except ValueError as exc:
                raise ValidationError("Invalid driver cash session status") from exc
            report_query = report_query.filter(DriverCashSession.status == status_enum)
        if driver_user_id:
            report_query = report_query.filter(DriverCashSession.driver_user_id == driver_user_id)
        if blocked_only:
            report_query = report_query.filter(DriverCashSession.blocked_from_cod.is_(True))
        if warning_only:
            report_query = self._apply_warning_only_filter(report_query)
        if min_session_age_days is not None:
            try:
                min_days = max(0, int(min_session_age_days))
            except (TypeError, ValueError) as exc:
                raise ValidationError("min_session_age_days must be an integer") from exc
            report_query = report_query.filter(
                DriverCashSession.session_started_at <= self._now_utc() - timedelta(days=min_days)
            )

        sessions = report_query.order_by(
            DriverCashSession.session_started_at.desc(),
            DriverCashSession.id.desc(),
        ).all()

        driver_rows: Dict[int, Dict[str, Any]] = defaultdict(
            lambda: {
                "driver_id": None,
                "driver_name": None,
                "phone": None,
                "delivery_count": 0,
                "total_cash_collected": 0.0,
                "expected_cash": 0.0,
                "expected_cash_on_hand": 0.0,
                "declared_cash": 0.0,
                "verified_cash": 0.0,
                "declared_variance": 0.0,
                "verified_variance": 0.0,
                "open_session_count": 0,
                "partial_session_count": 0,
                "submitted_session_count": 0,
                "verified_session_count": 0,
                "resolved_session_count": 0,
                "mismatch_session_count": 0,
                "overdue_session_count": 0,
                "warning_session_count": 0,
                "blocked_session_count": 0,
                "session_count": 0,
            }
        )
        summary = {
            "session_count": 0,
            "open_session_count": 0,
            "partial_session_count": 0,
            "submitted_session_count": 0,
            "verified_session_count": 0,
            "resolved_session_count": 0,
            "mismatch_session_count": 0,
            "overdue_session_count": 0,
            "warning_session_count": 0,
            "blocked_session_count": 0,
            "expected_cash_total": 0.0,
            "expected_cash_on_hand_total": 0.0,
            "declared_cash_total": 0.0,
            "verified_cash_total": 0.0,
            "declared_variance_total": 0.0,
            "verified_variance_total": 0.0,
            "driver_count": 0,
        }

        event_stats = self._event_stats_for_sessions([session.id for session in sessions])
        for session in sessions:
            driver = session.driver_user
            row = driver_rows[session.driver_user_id]
            if row["driver_id"] is None:
                row["driver_id"] = session.driver_user_id
                row["driver_name"] = driver.full_name if driver else None
                row["phone"] = driver.phone if driver else None

            row["session_count"] += 1
            row["total_cash_collected"] += float(session.gross_cash_collected or 0)
            row["expected_cash"] += float(session.expected_cash or 0)
            row["expected_cash_on_hand"] += float(session.expected_cash_on_hand or 0)
            row["declared_cash"] += float(session.declared_cash or 0)
            row["verified_cash"] += float(session.verified_cash or 0)
            row["declared_variance"] += float(session.declared_variance or 0)
            row["verified_variance"] += float(session.verified_variance or 0)
            row["delivery_count"] += int(event_stats.get(session.id, {}).get("delivery_count", 0))
            if session.status == DriverCashSessionStatus.OPEN:
                row["open_session_count"] += 1
                summary["open_session_count"] += 1
            if session.status == DriverCashSessionStatus.PARTIAL:
                row["partial_session_count"] += 1
                summary["partial_session_count"] += 1
            if session.status == DriverCashSessionStatus.SUBMITTED:
                row["submitted_session_count"] += 1
                summary["submitted_session_count"] += 1
            if session.status == DriverCashSessionStatus.VERIFIED:
                row["verified_session_count"] += 1
                summary["verified_session_count"] += 1
            if session.status == DriverCashSessionStatus.RESOLVED:
                row["resolved_session_count"] += 1
                summary["resolved_session_count"] += 1
            if session.status == DriverCashSessionStatus.MISMATCH:
                row["mismatch_session_count"] += 1
                summary["mismatch_session_count"] += 1
            is_warning_session = (
                not session.blocked_from_cod
                and self._status_value(session.status)
                in {
                    DriverCashSessionStatus.OPEN.value,
                    DriverCashSessionStatus.PARTIAL.value,
                    DriverCashSessionStatus.OVERDUE.value,
                }
                and self._is_warning_due(session)
            )
            if session.status == DriverCashSessionStatus.OVERDUE:
                row["overdue_session_count"] += 1
                summary["overdue_session_count"] += 1
            if is_warning_session:
                row["warning_session_count"] += 1
                summary["warning_session_count"] += 1
            if session.blocked_from_cod:
                row["blocked_session_count"] += 1
                summary["blocked_session_count"] += 1

            summary["session_count"] += 1
            summary["expected_cash_total"] += float(session.expected_cash or 0)
            summary["expected_cash_on_hand_total"] += float(session.expected_cash_on_hand or 0)
            summary["declared_cash_total"] += float(session.declared_cash or 0)
            summary["verified_cash_total"] += float(session.verified_cash or 0)
            summary["declared_variance_total"] += float(session.declared_variance or 0)
            summary["verified_variance_total"] += float(session.verified_variance or 0)

        summary["driver_count"] = len(driver_rows)
        report = sorted(
            driver_rows.values(),
            key=lambda item: ((item["blocked_session_count"] * -1), item["driver_name"] or ""),
        )

        return {
            "report": report,
            "sessions": sessions_result["items"],
            "summary": summary,
            "grand_total_cash": summary["expected_cash_total"],
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "page": sessions_result["page"],
            "per_page": sessions_result["per_page"],
            "total": sessions_result["total"],
        }
