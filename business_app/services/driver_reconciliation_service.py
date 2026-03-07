"""Service for driver COD reconciliation sessions and reporting."""

from collections import defaultdict
from datetime import date, datetime, UTC, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import joinedload

from business_app import db
from business_app.models.payment import CashCollectionEvent, DriverCashSession
from business_app.models.user import User
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from business_app.utils.constants import DriverCashSessionStatus
from business_app.utils.exceptions import NotFoundError, ValidationError


class DriverReconciliationService:
    """Driver cash reconciliation workflow service."""

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        if value is None:
            return Decimal('0.00')
        return Decimal(str(value)).quantize(Decimal('0.01'))

    @staticmethod
    def _normalize_business_date(value: Optional[Any]) -> date:
        if value is None:
            return datetime.now(UTC).date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))

    @staticmethod
    def _normalize_period_window(period: str) -> Tuple[date, date]:
        now = datetime.now(UTC)
        if period == 'day':
            start_date = now.date()
        elif period == 'week':
            start_date = (now - timedelta(days=6)).date()
        elif period == 'month':
            start_date = (now - timedelta(days=29)).date()
        else:
            raise ValidationError("Invalid reconciliation report period")
        return start_date, now.date()

    def _serialize_session(self, session: DriverCashSession, *, include_events: bool = False) -> Dict[str, Any]:
        payload = session.to_dict()
        driver = session.driver_user
        events = list(session.cash_collection_events or [])
        delivery_ids = {
            event.delivery_id
            for event in events
            if event.delivery_id
        }
        payload.update({
            'driver_name': driver.full_name if driver else None,
            'driver_phone': driver.phone if driver else None,
            'event_count': len(events),
            'delivery_count': len(delivery_ids),
            'total_cash_collected': float(session.expected_cash or 0),
        })
        if include_events:
            payload['events'] = [event.to_dict() for event in events]
        return payload

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
            )
            db.session.add(session)
            db.session.flush()

        self.refresh_expected_cash(session)
        return session

    def refresh_expected_cash(self, session: DriverCashSession) -> DriverCashSession:
        expected_cash = (
            db.session.query(db.func.coalesce(db.func.sum(CashCollectionEvent.amount), 0))
            .filter(
                CashCollectionEvent.driver_cash_session_id == session.id,
                CashCollectionEvent.voided_at.is_(None),
            )
            .scalar()
        )
        session.expected_cash = self._to_decimal(expected_cash)
        if session.declared_cash is not None:
            session.declared_variance = self._to_decimal(session.declared_cash) - self._to_decimal(session.expected_cash)
        if session.verified_cash is not None:
            session.verified_variance = self._to_decimal(session.verified_cash) - self._to_decimal(session.expected_cash)
        return session

    def submit_session(
        self,
        *,
        driver_user_id: int,
        declared_cash: Any,
        notes: Optional[str] = None,
        business_date: Optional[Any] = None,
        submitted_by_user_id: Optional[int] = None,
    ) -> DriverCashSession:
        session = self.get_or_create_session(
            driver_user_id=driver_user_id,
            business_date=business_date,
        )
        now = datetime.now(UTC)
        session.declared_cash = self._to_decimal(declared_cash)
        session.notes = notes
        session.submitted_at = now
        session.session_ended_at = now
        session.submitted_by_user_id = submitted_by_user_id or driver_user_id
        self.refresh_expected_cash(session)

        if session.declared_variance == Decimal('0.00'):
            session.status = DriverCashSessionStatus.SUBMITTED
            session.blocked_from_cod = False
            session.block_reason = None
        else:
            session.status = DriverCashSessionStatus.MISMATCH
            session.blocked_from_cod = True
            session.block_reason = 'declared_cash_mismatch'

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action='driver_cash_session_submitted',
            severity=AuditSeverity.MEDIUM if not session.blocked_from_cod else AuditSeverity.HIGH,
            resource_type='driver_cash_session',
            resource_id=str(session.id),
            additional_data={
                'driver_user_id': driver_user_id,
                'business_date': session.business_date.isoformat(),
                'expected_cash': float(session.expected_cash or 0),
                'declared_cash': float(session.declared_cash or 0),
                'declared_variance': float(session.declared_variance or 0),
                'blocked_from_cod': session.blocked_from_cod,
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
        notes: Optional[str] = None,
    ) -> DriverCashSession:
        session = DriverCashSession.query.options(joinedload(DriverCashSession.driver_user)).get(session_id)
        if not session:
            raise NotFoundError("Driver cash session not found")

        self.refresh_expected_cash(session)
        session.verified_cash = self._to_decimal(verified_cash)
        session.verified_at = datetime.now(UTC)
        session.verified_by_user_id = actor_user_id
        session.verification_notes = notes
        session.verified_variance = self._to_decimal(session.verified_cash) - self._to_decimal(session.expected_cash)

        if session.verified_variance == Decimal('0.00'):
            session.status = DriverCashSessionStatus.VERIFIED
            session.blocked_from_cod = False
            session.block_reason = None
        else:
            session.status = DriverCashSessionStatus.MISMATCH
            session.blocked_from_cod = True
            session.block_reason = 'verified_cash_mismatch'

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action='driver_cash_session_verified',
            severity=AuditSeverity.MEDIUM if not session.blocked_from_cod else AuditSeverity.HIGH,
            resource_type='driver_cash_session',
            resource_id=str(session.id),
            additional_data={
                'driver_user_id': session.driver_user_id,
                'verified_by_user_id': actor_user_id,
                'verified_cash': float(session.verified_cash or 0),
                'verified_variance': float(session.verified_variance or 0),
                'blocked_from_cod': session.blocked_from_cod,
            },
        )

        db.session.commit()
        return session

    def resolve_session(
        self,
        *,
        session_id: int,
        actor_user_id: int,
        resolution_notes: str,
        verified_cash: Optional[Any] = None,
    ) -> DriverCashSession:
        session = DriverCashSession.query.get(session_id)
        if not session:
            raise NotFoundError("Driver cash session not found")
        if not resolution_notes:
            raise ValidationError("Resolution notes are required")

        if verified_cash is not None:
            session.verified_cash = self._to_decimal(verified_cash)
            session.verified_variance = self._to_decimal(session.verified_cash) - self._to_decimal(session.expected_cash)
        session.verified_by_user_id = actor_user_id
        session.verified_at = datetime.now(UTC)
        session.status = DriverCashSessionStatus.RESOLVED
        session.blocked_from_cod = False
        session.block_reason = None
        session.resolution_notes = resolution_notes
        session.resolution_metadata = {
            'resolved_by_user_id': actor_user_id,
        }

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action='driver_cash_session_resolved',
            severity=AuditSeverity.HIGH,
            resource_type='driver_cash_session',
            resource_id=str(session.id),
            additional_data={
                'driver_user_id': session.driver_user_id,
                'resolved_by_user_id': actor_user_id,
                'resolution_notes': resolution_notes,
            },
        )

        db.session.commit()
        return session

    def mark_overdue_sessions(
        self,
        *,
        reference_time: Optional[datetime] = None,
    ) -> int:
        reference_time = reference_time or datetime.now(UTC)
        updated = 0
        sessions = DriverCashSession.query.filter(
            DriverCashSession.status.in_([
                DriverCashSessionStatus.OPEN,
                DriverCashSessionStatus.SUBMITTED,
            ]),
            DriverCashSession.business_date < reference_time.date(),
        ).all()

        for session in sessions:
            session.status = DriverCashSessionStatus.OVERDUE
            session.blocked_from_cod = True
            session.block_reason = 'reconciliation_overdue'
            updated += 1

        if updated:
            db.session.commit()
        return updated

    def is_driver_blocked_from_cod(self, driver_user_id: int) -> bool:
        blocked_session = DriverCashSession.query.filter(
            DriverCashSession.driver_user_id == driver_user_id,
            DriverCashSession.blocked_from_cod.is_(True),
            DriverCashSession.status.in_([
                DriverCashSessionStatus.MISMATCH,
                DriverCashSessionStatus.OVERDUE,
            ]),
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
        ).get(session_id)
        if not session:
            raise NotFoundError("Driver cash session not found")
        self.refresh_expected_cash(session)
        return self._serialize_session(session, include_events=True)

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
            'items': items,
            'page': pagination.page,
            'per_page': pagination.per_page,
            'total': pagination.total,
        }

    def get_report(
        self,
        *,
        period: str = 'day',
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

        driver_rows: Dict[int, Dict[str, Any]] = defaultdict(lambda: {
            'driver_id': None,
            'driver_name': None,
            'phone': None,
            'delivery_count': 0,
            'total_cash_collected': 0.0,
            'expected_cash': 0.0,
            'declared_cash': 0.0,
            'verified_cash': 0.0,
            'declared_variance': 0.0,
            'verified_variance': 0.0,
            'open_session_count': 0,
            'mismatch_session_count': 0,
            'overdue_session_count': 0,
            'blocked_session_count': 0,
            'session_count': 0,
        })
        summary = {
            'session_count': 0,
            'open_session_count': 0,
            'mismatch_session_count': 0,
            'overdue_session_count': 0,
            'blocked_session_count': 0,
            'expected_cash_total': 0.0,
            'declared_cash_total': 0.0,
            'verified_cash_total': 0.0,
            'declared_variance_total': 0.0,
            'verified_variance_total': 0.0,
            'driver_count': 0,
        }

        for session in sessions:
            self.refresh_expected_cash(session)
            driver = session.driver_user
            row = driver_rows[session.driver_user_id]
            if row['driver_id'] is None:
                row['driver_id'] = session.driver_user_id
                row['driver_name'] = driver.full_name if driver else None
                row['phone'] = driver.phone if driver else None

            row['session_count'] += 1
            row['total_cash_collected'] += float(session.expected_cash or 0)
            row['expected_cash'] += float(session.expected_cash or 0)
            row['declared_cash'] += float(session.declared_cash or 0)
            row['verified_cash'] += float(session.verified_cash or 0)
            row['declared_variance'] += float(session.declared_variance or 0)
            row['verified_variance'] += float(session.verified_variance or 0)
            row['delivery_count'] += len({
                event.delivery_id
                for event in session.cash_collection_events or []
                if event.delivery_id
            })
            if session.status == DriverCashSessionStatus.OPEN:
                row['open_session_count'] += 1
                summary['open_session_count'] += 1
            if session.status == DriverCashSessionStatus.MISMATCH:
                row['mismatch_session_count'] += 1
                summary['mismatch_session_count'] += 1
            if session.status == DriverCashSessionStatus.OVERDUE:
                row['overdue_session_count'] += 1
                summary['overdue_session_count'] += 1
            if session.blocked_from_cod:
                row['blocked_session_count'] += 1
                summary['blocked_session_count'] += 1

            summary['session_count'] += 1
            summary['expected_cash_total'] += float(session.expected_cash or 0)
            summary['declared_cash_total'] += float(session.declared_cash or 0)
            summary['verified_cash_total'] += float(session.verified_cash or 0)
            summary['declared_variance_total'] += float(session.declared_variance or 0)
            summary['verified_variance_total'] += float(session.verified_variance or 0)

        summary['driver_count'] = len(driver_rows)

        report = sorted(
            driver_rows.values(),
            key=lambda item: ((item['blocked_session_count'] * -1), item['driver_name'] or ''),
        )

        return {
            'report': report,
            'sessions': sessions_result['items'],
            'summary': summary,
            'grand_total_cash': summary['expected_cash_total'],
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat(),
            'page': sessions_result['page'],
            'per_page': sessions_result['per_page'],
            'total': sessions_result['total'],
        }
