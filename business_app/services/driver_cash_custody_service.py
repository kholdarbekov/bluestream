"""Service for checkpoint cash custody transfers within driver reconciliation sessions."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from business_app import db
from business_app.models.payment import DriverCashSession, DriverCashTransfer
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from shared.enums import DriverCashSessionStatus
from business_app.utils.exceptions import NotFoundError, ValidationError


class DriverCashCustodyService:
    """Handles creation and confirmation of checkpoint cash handoffs."""

    TRANSFER_STATUSES = {"pending", "confirmed", "disputed", "cancelled"}

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        if value is None:
            return Decimal("0.00")
        return Decimal(str(value)).quantize(Decimal("0.01"))

    @staticmethod
    def _validate_session_for_transfer(session: DriverCashSession) -> None:
        status = session.status.value if hasattr(session.status, "value") else str(session.status)
        if status not in {
            DriverCashSessionStatus.OPEN.value,
            DriverCashSessionStatus.SUBMITTED.value,
            DriverCashSessionStatus.OVERDUE.value,
            DriverCashSessionStatus.MISMATCH.value,
        }:
            raise ValidationError("Cannot create custody transfer for a closed reconciliation session")

    def create_transfer(
        self,
        *,
        session_id: int,
        driver_user_id: int,
        declared_transfer_cash: Any,
        notes: Optional[str] = None,
        transfer_metadata: Optional[Dict[str, Any]] = None,
    ) -> DriverCashTransfer:
        session = DriverCashSession.query.get(session_id)
        if not session:
            raise NotFoundError("Driver cash session not found")
        if session.driver_user_id != driver_user_id:
            raise ValidationError("Driver can only create custody transfers for their own session")

        self._validate_session_for_transfer(session)
        amount = self._to_decimal(declared_transfer_cash)
        if amount <= Decimal("0.00"):
            raise ValidationError("declared_transfer_cash must be greater than zero")

        transfer = DriverCashTransfer(
            driver_cash_session_id=session.id,
            driver_user_id=driver_user_id,
            declared_transfer_cash=amount,
            transfer_variance=Decimal("0.00"),
            transfer_status="pending",
            notes=notes,
            transfer_metadata=transfer_metadata or {},
            driver_confirmed_at=datetime.now(UTC),
            driver_confirmed_by_user_id=driver_user_id,
        )
        db.session.add(transfer)
        db.session.flush()

        from business_app.services.driver_reconciliation_service import DriverReconciliationService

        DriverReconciliationService().refresh_expected_cash(session)

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action="driver_cash_transfer_created",
            severity=AuditSeverity.MEDIUM,
            resource_type="driver_cash_transfer",
            resource_id=str(transfer.id),
            additional_data={
                "driver_cash_session_id": session.id,
                "driver_user_id": driver_user_id,
                "declared_transfer_cash": float(transfer.declared_transfer_cash or 0),
                "transfer_status": transfer.transfer_status,
            },
        )

        db.session.commit()
        return transfer

    def confirm_transfer(
        self,
        *,
        transfer_id: int,
        actor_user_id: int,
        counted_transfer_cash: Any,
        notes: Optional[str] = None,
        reason_code: Optional[str] = None,
    ) -> DriverCashTransfer:
        transfer = DriverCashTransfer.query.get(transfer_id)
        if not transfer:
            raise NotFoundError("Driver cash transfer not found")

        if transfer.transfer_status in {"confirmed", "disputed", "cancelled"}:
            raise ValidationError("Transfer is already closed")

        counted = self._to_decimal(counted_transfer_cash)
        if counted < Decimal("0.00"):
            raise ValidationError("counted_transfer_cash cannot be negative")

        transfer.counted_transfer_cash = counted
        transfer.transfer_variance = counted - self._to_decimal(transfer.declared_transfer_cash)
        transfer.checkpoint_confirmed_at = datetime.now(UTC)
        transfer.checkpoint_confirmed_by_user_id = actor_user_id
        transfer.transfer_status = "confirmed" if transfer.transfer_variance == Decimal("0.00") else "disputed"
        if notes:
            transfer.notes = notes

        metadata = dict(transfer.transfer_metadata or {})
        metadata["confirmed_by_user_id"] = actor_user_id
        if reason_code:
            metadata["reason_code"] = reason_code
        transfer.transfer_metadata = metadata

        from business_app.services.driver_reconciliation_service import DriverReconciliationService

        DriverReconciliationService().refresh_expected_cash(transfer.driver_cash_session)

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action="driver_cash_transfer_confirmed",
            severity=AuditSeverity.HIGH if transfer.transfer_status == "disputed" else AuditSeverity.MEDIUM,
            resource_type="driver_cash_transfer",
            resource_id=str(transfer.id),
            additional_data={
                "driver_cash_session_id": transfer.driver_cash_session_id,
                "driver_user_id": transfer.driver_user_id,
                "confirmed_by_user_id": actor_user_id,
                "declared_transfer_cash": float(transfer.declared_transfer_cash or 0),
                "counted_transfer_cash": float(transfer.counted_transfer_cash or 0),
                "transfer_variance": float(transfer.transfer_variance or 0),
                "transfer_status": transfer.transfer_status,
                "reason_code": reason_code,
            },
        )

        db.session.commit()
        return transfer
