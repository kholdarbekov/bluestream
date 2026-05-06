"""Service for driver COD reconciliation sessions and reporting."""

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from business_app import db
from business_app.models.payment import CashCollectionEvent, DriverCashSession, DriverCashTransfer
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
from business_app.utils.exceptions import NotFoundError, ValidationError


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
        "transfer_variance_settled",
        "clerical_correction",
        "other",
    }
    RESOLUTION_ALLOWED_STATUSES = {
        DriverCashSessionStatus.MISMATCH.value,
        DriverCashSessionStatus.OVERDUE.value,
    }

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        if value is None:
            return Decimal("0.00")
        return Decimal(str(value)).quantize(Decimal("0.01"))

    @staticmethod
    def _normalize_business_date(value: Optional[Any]) -> date:
        if value is None:
            tz = ZoneInfo(current_app.config.get("DISPLAY_TIMEZONE", "Asia/Tashkent"))
            return datetime.now(tz).date()
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

    def _get_cutoff_time(self) -> time:
        raw = str(current_app.config.get("COD_RECONCILIATION_CUTOFF_LOCAL", "23:00")).strip()
        try:
            hours, minutes = raw.split(":", 1)
            return time(hour=int(hours), minute=int(minutes), tzinfo=None)
        except (ValueError, TypeError) as exc:
            raise ValidationError("Invalid COD_RECONCILIATION_CUTOFF_LOCAL format. Expected HH:MM") from exc

    def _build_submission_due_at(self, business_date: date) -> datetime:
        tz = ZoneInfo(current_app.config.get("DISPLAY_TIMEZONE", "Asia/Tashkent"))
        cutoff = self._get_cutoff_time()
        local_dt = datetime.combine(
            business_date,
            time(hour=cutoff.hour, minute=cutoff.minute),
            tzinfo=tz,
        )
        return local_dt.astimezone(UTC)

    def _compute_transferred_cash_total(self, session_id: int) -> Decimal:
        transferred = (
            db.session.query(
                func.coalesce(
                    func.sum(
                        func.coalesce(
                            DriverCashTransfer.counted_transfer_cash,
                            DriverCashTransfer.declared_transfer_cash,
                        )
                    ),
                    0,
                )
            )
            .filter(
                DriverCashTransfer.driver_cash_session_id == session_id,
                DriverCashTransfer.transfer_status.in_(["confirmed", "disputed"]),
                DriverCashTransfer.checkpoint_confirmed_at.isnot(None),
            )
            .scalar()
        )
        return self._to_decimal(transferred)

    def _build_risk_flags(self, session: DriverCashSession) -> List[str]:
        flags: List[str] = []
        warning_threshold = self._to_decimal(current_app.config.get("COD_CASH_WARNING_THRESHOLD_UZS", 200000))
        escalation_threshold = self._to_decimal(current_app.config.get("COD_CASH_ESCALATION_THRESHOLD_UZS", 400000))

        on_hand = self._to_decimal(session.expected_cash_on_hand)
        if on_hand >= escalation_threshold:
            flags.append("cash_on_hand_escalation")
        elif on_hand >= warning_threshold:
            flags.append("cash_on_hand_warning")

        pending_transfer_count = (
            db.session.query(func.count(DriverCashTransfer.id))
            .filter(
                DriverCashTransfer.driver_cash_session_id == session.id,
                DriverCashTransfer.transfer_status == "pending",
            )
            .scalar()
            or 0
        )
        if pending_transfer_count > 0:
            flags.append("pending_transfer_confirmation")

        disputed_transfer_count = (
            db.session.query(func.count(DriverCashTransfer.id))
            .filter(
                DriverCashTransfer.driver_cash_session_id == session.id,
                DriverCashTransfer.transfer_status == "disputed",
            )
            .scalar()
            or 0
        )
        if disputed_transfer_count > 0:
            flags.append("transfer_variance_detected")

        rolling_window_start = self._now_utc().date() - timedelta(days=7)
        mismatch_count = (
            db.session.query(func.count(DriverCashSession.id))
            .filter(
                DriverCashSession.driver_user_id == session.driver_user_id,
                DriverCashSession.business_date >= rolling_window_start,
                DriverCashSession.status == DriverCashSessionStatus.MISMATCH,
            )
            .scalar()
            or 0
        )
        if mismatch_count >= 2:
            flags.append("repeated_mismatch_pattern")

        if self._status_value(session.status) == DriverCashSessionStatus.OVERDUE.value:
            flags.append("submission_overdue")

        due_at = self._as_aware_utc(session.submission_due_at)
        if due_at and self._now_utc() > due_at and not session.submitted_at:
            flags.append("missed_cutoff")

        return sorted(set(flags))

    def _build_next_actions(self, session: DriverCashSession) -> List[str]:
        status = self._status_value(session.status)
        actions: List[str] = []
        if status in {DriverCashSessionStatus.OPEN.value, DriverCashSessionStatus.OVERDUE.value}:
            actions.append("submit_reconciliation")
            actions.append("create_checkpoint_handoff")
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
        include_transfers: bool = False,
    ) -> Dict[str, Any]:
        payload = session.to_dict()
        driver = session.driver_user
        events = list(session.cash_collection_events or [])
        transfers = list(session.cash_transfers or [])
        delivery_ids = {event.delivery_id for event in events if event.delivery_id}
        payload.update(
            {
                "driver_name": driver.full_name if driver else None,
                "driver_phone": driver.phone if driver else None,
                "event_count": len(events),
                "transfer_count": len(transfers),
                "delivery_count": len(delivery_ids),
                "total_cash_collected": float(session.gross_cash_collected or 0),
                "next_actions": self._build_next_actions(session),
            }
        )
        if include_events:
            payload["events"] = [event.to_dict() for event in events]
        if include_transfers:
            payload["transfers"] = [transfer.to_dict() for transfer in transfers]
        return payload

    def _assert_session_transition(self, session: DriverCashSession, *, operation: str) -> None:
        status = self._status_value(session.status)
        allowed = {
            "submit": {
                DriverCashSessionStatus.OPEN.value,
                DriverCashSessionStatus.SUBMITTED.value,
                DriverCashSessionStatus.OVERDUE.value,
            },
            "verify": {
                DriverCashSessionStatus.OPEN.value,
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
        business_date: Optional[Any] = None,
    ) -> DriverCashSession:
        driver = User.query.get(driver_user_id)
        if not driver:
            raise NotFoundError("Driver not found")

        normalized_date = self._normalize_business_date(business_date)
        session = DriverCashSession.query.filter_by(
            driver_user_id=driver_user_id,
            business_date=normalized_date,
        ).first()
        if not session:
            session = DriverCashSession(
                driver_user_id=driver_user_id,
                business_date=normalized_date,
                status=DriverCashSessionStatus.OPEN,
                submission_due_at=self._build_submission_due_at(normalized_date),
                reminder_stage="none",
            )
            db.session.add(session)
            db.session.flush()

        self.refresh_expected_cash(session)
        return session

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
        transferred_total = self._compute_transferred_cash_total(session.id)
        expected_on_hand = gross - transferred_total

        session.expected_cash = gross
        session.gross_cash_collected = gross
        session.transferred_cash_total = transferred_total
        session.expected_cash_on_hand = expected_on_hand
        session.submission_due_at = session.submission_due_at or self._build_submission_due_at(session.business_date)

        if session.declared_cash is not None:
            session.declared_variance = self._to_decimal(session.declared_cash) - expected_on_hand
        else:
            session.declared_variance = Decimal("0.00")
        if session.verified_cash is not None:
            session.verified_variance = self._to_decimal(session.verified_cash) - expected_on_hand
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
        business_date: Optional[Any] = None,
        submitted_by_user_id: Optional[int] = None,
    ) -> DriverCashSession:
        session = self.get_or_create_session(
            driver_user_id=driver_user_id,
            business_date=business_date,
        )
        self._assert_session_transition(session, operation="submit")

        now = self._now_utc()
        self.refresh_expected_cash(session)
        if declared_cash is None:
            session.declared_cash = self._to_decimal(session.expected_cash_on_hand)
        else:
            session.declared_cash = self._to_decimal(declared_cash)

        session.notes = notes
        session.submitted_at = now
        session.session_ended_at = now
        session.submitted_by_user_id = submitted_by_user_id or driver_user_id
        self.refresh_expected_cash(session)

        if session.declared_variance == Decimal("0.00"):
            session.status = DriverCashSessionStatus.SUBMITTED
            session.blocked_from_cod = False
            session.block_reason = None
        else:
            session.status = DriverCashSessionStatus.MISMATCH
            session.blocked_from_cod = True
            session.block_reason = "declared_cash_mismatch"

        session.risk_flags = self._build_risk_flags(session)

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action="driver_cash_session_submitted",
            severity=AuditSeverity.MEDIUM if not session.blocked_from_cod else AuditSeverity.HIGH,
            resource_type="driver_cash_session",
            resource_id=str(session.id),
            additional_data={
                "driver_user_id": driver_user_id,
                "business_date": session.business_date.isoformat(),
                "expected_cash": float(session.expected_cash or 0),
                "expected_cash_on_hand": float(session.expected_cash_on_hand or 0),
                "declared_cash": float(session.declared_cash or 0),
                "declared_variance": float(session.declared_variance or 0),
                "blocked_from_cod": session.blocked_from_cod,
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
            joinedload(DriverCashSession.cash_transfers),
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

    def mark_overdue_sessions(
        self,
        *,
        reference_time: Optional[datetime] = None,
    ) -> int:
        reference_time = self._as_aware_utc(reference_time) or self._now_utc()
        updated = 0
        sessions = DriverCashSession.query.filter(
            DriverCashSession.status.in_(
                [
                    DriverCashSessionStatus.OPEN,
                    DriverCashSessionStatus.SUBMITTED,
                ]
            ),
        ).all()

        for session in sessions:
            self.refresh_expected_cash(session)
            if session.submitted_at:
                continue
            due_at = self._as_aware_utc(session.submission_due_at) or self._build_submission_due_at(
                session.business_date
            )
            if due_at <= reference_time:
                session.status = DriverCashSessionStatus.OVERDUE
                session.blocked_from_cod = True
                session.block_reason = "reconciliation_overdue"
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
                        DriverCashSessionStatus.SUBMITTED,
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

            due_at = self._as_aware_utc(session.submission_due_at)
            stage = "overdue" if due_at and reference_time >= due_at else "pre_cutoff"
            due_sessions.append((session, stage))

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
            date=session.business_date.isoformat(),
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
                date=session.business_date.isoformat(),
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
                "business_date": session.business_date.isoformat(),
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
            DriverCashSession.status.in_(
                [
                    DriverCashSessionStatus.MISMATCH,
                    DriverCashSessionStatus.OVERDUE,
                ]
            ),
        ).first()
        return blocked_session is not None

    def get_open_session_for_driver(
        self,
        driver_user_id: int,
        *,
        business_date: Optional[Any] = None,
    ) -> DriverCashSession:
        return self.get_or_create_session(
            driver_user_id=driver_user_id,
            business_date=business_date,
        )

    def get_session_detail(self, session_id: int) -> Dict[str, Any]:
        session = DriverCashSession.query.options(
            joinedload(DriverCashSession.driver_user),
            joinedload(DriverCashSession.cash_collection_events),
            joinedload(DriverCashSession.cash_transfers),
        ).get(session_id)
        if not session:
            raise NotFoundError("Driver cash session not found")
        self.refresh_expected_cash(session)
        return self._serialize_session(session, include_events=True, include_transfers=True)

    def list_sessions(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        status: Optional[str] = None,
        driver_user_id: Optional[int] = None,
        business_date: Optional[Any] = None,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        blocked_only: bool = False,
    ) -> Dict[str, Any]:
        query = DriverCashSession.query.options(
            joinedload(DriverCashSession.driver_user),
            joinedload(DriverCashSession.cash_collection_events),
            joinedload(DriverCashSession.cash_transfers),
        )

        if status:
            try:
                status_enum = DriverCashSessionStatus(status)
            except ValueError as exc:
                raise ValidationError("Invalid driver cash session status") from exc
            query = query.filter(DriverCashSession.status == status_enum)
        if driver_user_id:
            query = query.filter(DriverCashSession.driver_user_id == driver_user_id)
        if business_date:
            query = query.filter(DriverCashSession.business_date == self._normalize_business_date(business_date))
        if start_date:
            query = query.filter(DriverCashSession.business_date >= self._normalize_business_date(start_date))
        if end_date:
            query = query.filter(DriverCashSession.business_date <= self._normalize_business_date(end_date))
        if blocked_only:
            query = query.filter(DriverCashSession.blocked_from_cod.is_(True))

        pagination = query.order_by(
            DriverCashSession.business_date.desc(),
            DriverCashSession.created_at.desc(),
        ).paginate(page=page, per_page=min(per_page, 100), error_out=False)

        items = []
        for session in pagination.items:
            self.refresh_expected_cash(session)
            items.append(self._serialize_session(session))

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
    ) -> Dict[str, Any]:
        start_date, end_date = self._normalize_period_window(period)
        sessions_result = self.list_sessions(
            page=page,
            per_page=per_page,
            status=status,
            driver_user_id=driver_user_id,
            start_date=start_date,
            end_date=end_date,
            blocked_only=blocked_only,
        )

        report_query = DriverCashSession.query.options(
            joinedload(DriverCashSession.driver_user),
            joinedload(DriverCashSession.cash_collection_events),
            joinedload(DriverCashSession.cash_transfers),
        ).filter(
            DriverCashSession.business_date >= start_date,
            DriverCashSession.business_date <= end_date,
        )
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

        sessions = report_query.order_by(
            DriverCashSession.business_date.desc(),
            DriverCashSession.created_at.desc(),
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
                "mismatch_session_count": 0,
                "overdue_session_count": 0,
                "blocked_session_count": 0,
                "session_count": 0,
            }
        )
        summary = {
            "session_count": 0,
            "open_session_count": 0,
            "mismatch_session_count": 0,
            "overdue_session_count": 0,
            "blocked_session_count": 0,
            "expected_cash_total": 0.0,
            "expected_cash_on_hand_total": 0.0,
            "declared_cash_total": 0.0,
            "verified_cash_total": 0.0,
            "declared_variance_total": 0.0,
            "verified_variance_total": 0.0,
            "driver_count": 0,
        }

        for session in sessions:
            self.refresh_expected_cash(session)
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
            row["delivery_count"] += len(
                {event.delivery_id for event in session.cash_collection_events or [] if event.delivery_id}
            )
            if session.status == DriverCashSessionStatus.OPEN:
                row["open_session_count"] += 1
                summary["open_session_count"] += 1
            if session.status == DriverCashSessionStatus.MISMATCH:
                row["mismatch_session_count"] += 1
                summary["mismatch_session_count"] += 1
            if session.status == DriverCashSessionStatus.OVERDUE:
                row["overdue_session_count"] += 1
                summary["overdue_session_count"] += 1
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
