from datetime import datetime, timedelta, UTC
from decimal import Decimal
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    Text,
    ForeignKey,
    Enum,
    JSON,
    Index,
    Numeric,
    UniqueConstraint,
    text as sa_text,
)
from sqlalchemy.orm import relationship, backref
import uuid
from business_app import db
from shared.enums import (
    PaymentMethod,
    PaymentStatus,
    FiscalizationStatus,
    CashCollectionSource,
    DriverCashSessionStatus,
)
from business_app.models import TimestampMixin


class Payment(db.Model, TimestampMixin):
    __tablename__ = "payments"
    __table_args__ = (
        Index("idx_payments_user_status", "user_id", "status"),
        Index("idx_payments_status_created", "status", "created_at"),
        Index("idx_payments_method_status", "payment_method", "status"),
        Index("idx_payments_outstanding_status", "outstanding_amount", "status"),
    )

    id = Column(Integer, primary_key=True)
    payment_id = Column(String(100), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Canonical payment contract: at most one payment record may reference a given order.
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True, unique=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=True)
    # PAY-005: stable per-attempt idempotency key derived from
    # (order_id, user_id, amount, payment_method). Lets the service layer
    # short-circuit duplicate creation requests (customer refresh, double-tap)
    # cleanly with a SELECT instead of relying on an IntegrityError. Nullable
    # to support backfill of historical rows; new rows always set it.
    idempotency_key = Column(String(64), unique=True, nullable=True, index=True)

    amount = Column(Numeric(precision=10, scale=2), nullable=False)
    currency = Column(String(3), default="UZS")
    payment_method = Column(
        Enum(PaymentMethod, name="payment_method", values_callable=lambda x: [e.value for e in x]), nullable=False
    )
    status = Column(
        Enum(PaymentStatus, name="payment_status", values_callable=lambda x: [e.value for e in x]),
        default=PaymentStatus.PENDING,
        index=True,
    )

    # Payment provider specific data
    provider_transaction_id = Column(String(255), nullable=True, index=True)
    provider_data = Column(JSON, default={})

    # Payment link details (for Payme/Click)
    payment_link = Column(String(500), nullable=True)
    payment_link_expires_at = Column(DateTime(timezone=True), nullable=True)

    # Webhook processing
    webhook_processed = Column(Boolean, default=False)
    webhook_attempts = Column(Integer, default=0)

    # Payment completion timestamp
    paid_at = Column(DateTime(timezone=True), nullable=True)
    collected_by = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    amount_collected = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"))
    outstanding_amount = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"), index=True)
    last_collected_at = Column(DateTime(timezone=True), nullable=True)

    # Metadata
    description = Column(String(255), nullable=True)
    callback_url = Column(String(500), nullable=True)
    failure_reason = Column(String(500), nullable=True)
    consume_marking_codes = Column(Boolean, nullable=False, default=False)

    user = relationship("User", foreign_keys=[user_id], back_populates="payments")
    order = relationship("Order", back_populates="payment")
    subscription = relationship("Subscription", back_populates="payments")
    collected_by_user = relationship("User", foreign_keys=[collected_by])
    cash_collection_allocations = relationship(
        "CashCollectionAllocation",
        back_populates="payment",
        cascade="all, delete-orphan",
    )
    fiscalization = relationship(
        "PaymentFiscalization",
        back_populates="payment",
        uselist=False,
        cascade="all, delete-orphan",
    )
    marking_code_allocations = relationship(
        "OrderItemMarkingCodeAllocation",
        back_populates="payment",
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.payment_id:
            self.payment_id = str(uuid.uuid4())
        amount = Decimal(str(self.amount or 0))
        amount_collected = Decimal(str(self.amount_collected or 0))
        if self.outstanding_amount is None:
            self.outstanding_amount = max(Decimal("0.00"), amount - amount_collected)
        # PAY-005: auto-derive idempotency_key when caller didn't supply one
        # AND the constituent fields are all known. Skipping derivation when
        # any is missing keeps __init__ tolerant of partial construction
        # (admin tooling, tests) — the service layer always sets them.
        if (
            not self.idempotency_key
            and self.order_id is not None
            and self.user_id is not None
            and self.amount is not None
            and self.payment_method is not None
        ):
            self.idempotency_key = self.compute_idempotency_key(
                order_id=self.order_id,
                user_id=self.user_id,
                amount=self.amount,
                payment_method=self.payment_method,
            )

    @staticmethod
    def compute_idempotency_key(*, order_id, user_id, amount, payment_method) -> str:
        """Stable per-attempt key for de-duping payment creation requests.

        Hashes (order_id, user_id, normalised amount, normalised payment_method).
        Two requests with the same tuple resolve to the same key — duplicate
        creation collapses into the existing row instead of either failing
        with IntegrityError on the order_id unique constraint or (worse) a
        race that creates two rows on different replicas before commit.
        """
        import hashlib

        method_value = payment_method.value if hasattr(payment_method, "value") else str(payment_method)
        # Normalise amount to a fixed-precision string so 100 vs 100.00 hash same.
        amount_normalised = format(Decimal(str(amount)), "f")
        material = f"{order_id}:{user_id}:{amount_normalised}:{method_value}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def is_expired(self):
        """Check if payment link is expired"""
        if self.payment_link_expires_at:
            return datetime.now(UTC) > self.payment_link_expires_at
        return False

    @property
    def is_settled(self) -> bool:
        return Decimal(str(self.outstanding_amount or 0)) <= Decimal("0.00")

    @property
    def payment_provider(self) -> str:
        method_value = self.payment_method.value if hasattr(self.payment_method, "value") else self.payment_method
        if method_value == PaymentMethod.CARD.value:
            return PaymentMethod.CLICK.value
        return method_value

    @property
    def gateway_reference(self):
        return self.provider_transaction_id

    @gateway_reference.setter
    def gateway_reference(self, value):
        self.provider_transaction_id = value

    @property
    def gateway_response(self):
        return self.provider_data or {}

    @gateway_response.setter
    def gateway_response(self, value):
        self.provider_data = value or {}

    @property
    def refunded_amount(self):
        return Decimal(str((self.provider_data or {}).get("refunded_amount") or 0))

    @property
    def refunded_at(self):
        value = (self.provider_data or {}).get("refunded_at")
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            normalized = str(value).replace("Z", "+00:00")
            return datetime.fromisoformat(normalized)
        except (TypeError, ValueError):
            return None

    def to_dict(self):
        return {
            "id": self.id,
            "payment_id": self.payment_id,
            "amount": self.amount,
            "currency": self.currency,
            "payment_method": self.payment_method.value,
            "status": self.status.value,
            "payment_link": self.payment_link,
            "payment_provider": self.payment_provider,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
            "last_collected_at": self.last_collected_at.isoformat() if self.last_collected_at else None,
            "order_id": self.order_id,
            "subscription_id": self.subscription_id,
            "amount_collected": self.amount_collected,
            "outstanding_amount": self.outstanding_amount,
            "consume_marking_codes": bool(self.consume_marking_codes),
            "fiscalization_status": (
                self.fiscalization.status.value
                if self.fiscalization and hasattr(self.fiscalization.status, "value")
                else None
            ),
        }


class PaymentTransaction(db.Model, TimestampMixin):
    """Track individual payment transactions and events"""

    __tablename__ = "payment_transactions"

    id = Column(Integer, primary_key=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=False, index=True)
    transaction_type = Column(String(50), nullable=False)  # charge, refund, capture, cancel

    # Transaction details
    amount = Column(Numeric(precision=10, scale=2), nullable=False)
    currency = Column(String(3), default="UZS")
    status = Column(String(20), nullable=False)  # success, failed, pending

    # External provider details
    provider_transaction_id = Column(String(255), nullable=True, index=True)
    provider_reference = Column(String(255), nullable=True)
    provider_response = Column(JSON, default={})

    # Transaction context
    initiated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)

    # Result details
    success = Column(Boolean, nullable=False, default=False)
    failure_reason = Column(String(500), nullable=True)

    # Processing details
    processed_at = Column(DateTime(timezone=True), nullable=True)
    processing_time_ms = Column(Integer, nullable=True)

    # Additional data
    extra_data = Column(JSON, default={})
    notes = Column(Text, nullable=True)

    payment = relationship("Payment", backref="transactions")
    initiated_by_user = relationship("User", foreign_keys=[initiated_by])

    @property
    def gateway_response(self):
        return self.provider_response or {}

    def to_dict(self):
        return {
            "id": self.id,
            "payment_id": self.payment_id,
            "transaction_type": self.transaction_type,
            "amount": self.amount,
            "currency": self.currency,
            "status": self.status,
            "provider_transaction_id": self.provider_transaction_id,
            "success": self.success,
            "failure_reason": self.failure_reason,
            "processed_at": self.processed_at.isoformat() if self.processed_at else None,
            "processing_time_ms": self.processing_time_ms,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "initiated_by": self.initiated_by,
            "notes": self.notes,
        }


class PaymentFiscalization(db.Model, TimestampMixin):
    """Canonical fiscalization state and provider receipt payload for a payment."""

    __tablename__ = "payment_fiscalizations"
    __table_args__ = (
        Index("idx_payment_fiscalizations_status_attempt", "status", "last_attempt_at"),
        Index("idx_payment_fiscalizations_provider_receipt", "provider_receipt_id"),
        Index("idx_payment_fiscalizations_retries_exhausted", "retries_exhausted_at"),
    )

    id = Column(Integer, primary_key=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=False, unique=True, index=True)
    status = Column(
        Enum(
            FiscalizationStatus,
            name="payment_fiscalization_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=FiscalizationStatus.PENDING,
        index=True,
    )
    provider_name = Column(String(32), nullable=False, default="click")
    provider_receipt_id = Column(String(255), nullable=True, index=True)
    provider_receipt_url = Column(String(500), nullable=True)
    provider_status = Column(String(64), nullable=True)
    request_payload = Column(JSON, nullable=False, default=dict)
    response_payload = Column(JSON, nullable=False, default=dict)
    receipt_payload = Column(JSON, nullable=False, default=dict)
    failure_reason = Column(String(500), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    queued_at = Column(DateTime(timezone=True), nullable=True)
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    next_retry_at = Column(DateTime(timezone=True), nullable=True)
    tax_committee_utilised_at = Column(DateTime(timezone=True), nullable=True)
    retries_exhausted_at = Column(DateTime(timezone=True), nullable=True)

    payment = relationship("Payment", back_populates="fiscalization")
    marking_code_allocations = relationship(
        "OrderItemMarkingCodeAllocation",
        back_populates="payment_fiscalization",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "payment_id": self.payment_id,
            "status": self.status.value if hasattr(self.status, "value") else self.status,
            "provider_name": self.provider_name,
            "provider_receipt_id": self.provider_receipt_id,
            "provider_receipt_url": self.provider_receipt_url,
            "provider_status": self.provider_status,
            "request_payload": self.request_payload or {},
            "response_payload": self.response_payload or {},
            "receipt_payload": self.receipt_payload or {},
            "failure_reason": self.failure_reason,
            "attempts": self.attempts,
            "queued_at": self.queued_at.isoformat() if self.queued_at else None,
            "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
            "tax_committee_utilised_at": (
                self.tax_committee_utilised_at.isoformat() if self.tax_committee_utilised_at else None
            ),
            "retries_exhausted_at": (self.retries_exhausted_at.isoformat() if self.retries_exhausted_at else None),
        }


class TaxCommitteeApiToken(db.Model, TimestampMixin):
    """Stores Tax Committee (xTrace / Asl Belgisi) API bearer tokens.

    Only one row should have is_active=True at any time. Tokens expire
    every ~90 days and are refreshed via the Tax Committee refresh endpoint.
    """

    __tablename__ = "tax_committee_api_tokens"

    id = Column(Integer, primary_key=True)
    token = Column(Text, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    last_refreshed_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "is_active": self.is_active,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "last_checked_at": self.last_checked_at.isoformat() if self.last_checked_at else None,
            "last_refreshed_at": self.last_refreshed_at.isoformat() if self.last_refreshed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class DriverCashSession(db.Model, TimestampMixin):
    """Driver COD cash custody session, closed when the driver reconciles cash."""

    __tablename__ = "driver_cash_sessions"
    __table_args__ = (
        Index("idx_driver_cash_sessions_driver_started", "driver_user_id", "session_started_at"),
        Index("idx_driver_cash_sessions_status_started", "status", "session_started_at"),
        Index("idx_driver_cash_sessions_warning_due", "warning_due_at"),
        Index("idx_driver_cash_sessions_status_warning", "status", "warning_due_at"),
        Index("idx_driver_cash_sessions_session_ended", "session_ended_at"),
        Index(
            "uq_driver_cash_sessions_driver_active",
            "driver_user_id",
            unique=True,
            postgresql_where=sa_text("status IN ('open', 'overdue')"),
            sqlite_where=sa_text("status IN ('open', 'overdue')"),
        ),
    )

    id = Column(Integer, primary_key=True)
    session_id = Column(String(100), unique=True, nullable=False, index=True)
    driver_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    status = Column(
        Enum(
            DriverCashSessionStatus,
            name="driver_cash_session_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=DriverCashSessionStatus.OPEN,
        index=True,
    )
    session_started_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    session_ended_at = Column(DateTime(timezone=True), nullable=True)
    expected_cash = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"))
    gross_cash_collected = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"))
    expected_cash_on_hand = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"))
    declared_cash = Column(Numeric(precision=12, scale=2), nullable=True)
    verified_cash = Column(Numeric(precision=12, scale=2), nullable=True)
    declared_variance = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"))
    verified_variance = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"))
    submission_due_at = Column(DateTime(timezone=True), nullable=True)
    warning_due_at = Column(DateTime(timezone=True), nullable=True)
    last_cash_activity_at = Column(DateTime(timezone=True), nullable=True)
    last_reminder_at = Column(DateTime(timezone=True), nullable=True)
    reminder_stage = Column(String(32), nullable=False, default="none")
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    submitted_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    verified_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    blocked_from_cod = Column(Boolean, nullable=False, default=False, index=True)
    block_reason = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)
    verification_notes = Column(Text, nullable=True)
    resolution_notes = Column(Text, nullable=True)
    verification_reason_code = Column(String(64), nullable=True)
    resolution_reason_code = Column(String(64), nullable=True)
    risk_flags = Column(JSON, nullable=False, default=list)
    resolution_metadata = Column(JSON, nullable=True, default=dict)
    # Reopen audit fields: admin can reopen a SUBMITTED/VERIFIED/RESOLVED/MISMATCH
    # session when an order edit retroactively changes cash collected. The
    # session goes back to OPEN; the driver re-submits and an admin re-verifies.
    reopened_at = Column(DateTime(timezone=True), nullable=True)
    reopened_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    reopened_reason = Column(String(255), nullable=True)
    reopen_count = Column(Integer, nullable=False, default=0, server_default=sa_text("0"))

    driver_user = relationship("User", foreign_keys=[driver_user_id], backref="driver_cash_sessions")
    reopened_by_user = relationship("User", foreign_keys=[reopened_by_user_id])
    submitted_by_user = relationship("User", foreign_keys=[submitted_by_user_id])
    verified_by_user = relationship("User", foreign_keys=[verified_by_user_id])
    cash_collection_events = relationship(
        "CashCollectionEvent",
        back_populates="driver_cash_session",
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.session_id:
            self.session_id = str(uuid.uuid4())

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "driver_user_id": self.driver_user_id,
            "status": self.status.value if hasattr(self.status, "value") else self.status,
            "session_started_at": self.session_started_at.isoformat() if self.session_started_at else None,
            "session_ended_at": self.session_ended_at.isoformat() if self.session_ended_at else None,
            "expected_cash": float(self.expected_cash or 0),
            "gross_cash_collected": float(self.gross_cash_collected or 0),
            "expected_cash_on_hand": float(self.expected_cash_on_hand or 0),
            "declared_cash": float(self.declared_cash) if self.declared_cash is not None else None,
            "verified_cash": float(self.verified_cash) if self.verified_cash is not None else None,
            "declared_variance": float(self.declared_variance or 0),
            "verified_variance": float(self.verified_variance or 0),
            "submission_due_at": self.submission_due_at.isoformat() if self.submission_due_at else None,
            "warning_due_at": self.warning_due_at.isoformat() if self.warning_due_at else None,
            "last_cash_activity_at": (self.last_cash_activity_at.isoformat() if self.last_cash_activity_at else None),
            "last_reminder_at": self.last_reminder_at.isoformat() if self.last_reminder_at else None,
            "reminder_stage": self.reminder_stage,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "blocked_from_cod": self.blocked_from_cod,
            "block_reason": self.block_reason,
            "notes": self.notes,
            "verification_notes": self.verification_notes,
            "verification_reason_code": self.verification_reason_code,
            "resolution_notes": self.resolution_notes,
            "resolution_reason_code": self.resolution_reason_code,
            "risk_flags": list(self.risk_flags or []),
            "resolution_metadata": self.resolution_metadata or {},
            "reopened_at": self.reopened_at.isoformat() if self.reopened_at else None,
            "reopened_by_user_id": self.reopened_by_user_id,
            "reopened_reason": self.reopened_reason,
            "reopen_count": self.reopen_count or 0,
        }


class CashCollectionEvent(db.Model, TimestampMixin):
    """Cash collection event for COD receivables."""

    __tablename__ = "cash_collection_events"
    __table_args__ = (
        Index("idx_cash_collection_events_customer_created", "customer_id", "created_at"),
        Index("idx_cash_collection_events_collector_occurred", "collector_user_id", "occurred_at"),
        Index("idx_cash_collection_events_session_occurred", "driver_cash_session_id", "occurred_at"),
        Index("idx_cash_collection_events_source_occurred", "source", "occurred_at"),
        UniqueConstraint("idempotency_key", name="uq_cash_collection_events_idempotency_key"),
    )

    id = Column(Integer, primary_key=True)
    event_id = Column(String(100), unique=True, nullable=False, index=True)
    customer_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    collector_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    recorded_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True, index=True)
    delivery_id = Column(Integer, ForeignKey("deliveries.id"), nullable=True, index=True)
    driver_cash_session_id = Column(Integer, ForeignKey("driver_cash_sessions.id"), nullable=True, index=True)
    amount = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"))
    currency = Column(String(3), nullable=False, default="UZS")
    source = Column(
        Enum(
            CashCollectionSource,
            name="cash_collection_source",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        index=True,
    )
    occurred_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True)
    notes = Column(Text, nullable=True)
    proof_data = Column(JSON, nullable=True, default=dict)
    unapplied_amount = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"))
    idempotency_key = Column(String(255), nullable=True)
    voided_at = Column(DateTime(timezone=True), nullable=True)
    voided_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    void_reason = Column(String(255), nullable=True)
    entry_metadata = Column(JSON, nullable=True, default=dict)

    customer = relationship("User", foreign_keys=[customer_id], backref="cash_collection_events")
    collector_user = relationship("User", foreign_keys=[collector_user_id])
    recorded_by_user = relationship("User", foreign_keys=[recorded_by_user_id])
    voided_by_user = relationship("User", foreign_keys=[voided_by_user_id])
    order = relationship("Order", backref=backref("cash_collection_events", lazy="dynamic"))
    delivery = relationship("Delivery", backref=backref("cash_collection_events", lazy="dynamic"))
    driver_cash_session = relationship("DriverCashSession", back_populates="cash_collection_events")
    allocations = relationship(
        "CashCollectionAllocation",
        back_populates="cash_collection_event",
        cascade="all, delete-orphan",
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.event_id:
            self.event_id = str(uuid.uuid4())

    def to_dict(self):
        return {
            "id": self.id,
            "event_id": self.event_id,
            "customer_id": self.customer_id,
            "collector_user_id": self.collector_user_id,
            "recorded_by_user_id": self.recorded_by_user_id,
            "order_id": self.order_id,
            "delivery_id": self.delivery_id,
            "driver_cash_session_id": self.driver_cash_session_id,
            "amount": float(self.amount or 0),
            "currency": self.currency,
            "source": self.source.value if hasattr(self.source, "value") else self.source,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "notes": self.notes,
            "proof_data": self.proof_data or {},
            "unapplied_amount": float(self.unapplied_amount or 0),
            "voided_at": self.voided_at.isoformat() if self.voided_at else None,
            "voided_by_user_id": self.voided_by_user_id,
            "void_reason": self.void_reason,
            "entry_metadata": self.entry_metadata or {},
        }


class CashCollectionAllocation(db.Model, TimestampMixin):
    """Allocation of a cash collection event to an order payment."""

    __tablename__ = "cash_collection_allocations"
    __table_args__ = (
        Index("idx_cash_collection_allocations_payment_created", "payment_id", "created_at"),
        Index("idx_cash_collection_allocations_event_created", "cash_collection_event_id", "created_at"),
        UniqueConstraint(
            "cash_collection_event_id",
            "payment_id",
            "allocation_order",
            name="uq_cash_collection_allocations_event_payment_order",
        ),
    )

    id = Column(Integer, primary_key=True)
    cash_collection_event_id = Column(Integer, ForeignKey("cash_collection_events.id"), nullable=False, index=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True, index=True)
    allocated_amount = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"))
    allocation_order = Column(Integer, nullable=False, default=1)
    allocation_mode = Column(String(20), nullable=False, default="auto")
    allocated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    reversed_at = Column(DateTime(timezone=True), nullable=True)
    reversed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    reversal_reason = Column(String(255), nullable=True)
    allocation_metadata = Column(JSON, nullable=True, default=dict)

    cash_collection_event = relationship("CashCollectionEvent", back_populates="allocations")
    payment = relationship("Payment", back_populates="cash_collection_allocations")
    order = relationship("Order", backref=backref("cash_collection_allocations", lazy="dynamic"))
    reversed_by_user = relationship("User", foreign_keys=[reversed_by_user_id])

    def to_dict(self):
        return {
            "id": self.id,
            "cash_collection_event_id": self.cash_collection_event_id,
            "payment_id": self.payment_id,
            "order_id": self.order_id,
            "allocated_amount": float(self.allocated_amount or 0),
            "allocation_order": self.allocation_order,
            "allocation_mode": self.allocation_mode,
            "allocated_at": self.allocated_at.isoformat() if self.allocated_at else None,
            "reversed_at": self.reversed_at.isoformat() if self.reversed_at else None,
            "reversed_by_user_id": self.reversed_by_user_id,
            "reversal_reason": self.reversal_reason,
            "allocation_metadata": self.allocation_metadata or {},
        }


class CreditCard(db.Model, TimestampMixin):
    """User credit card information"""

    __tablename__ = "credit_cards"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Card details (encrypted/tokenized)
    card_token = Column(String(511), unique=True, nullable=False, index=True)  # Provider token
    card_brand = Column(String(20), nullable=False)  # visa, mastercard, uzcard
    last_four_digits = Column(String(4), nullable=False)
    expiry_month = Column(Integer, nullable=False)
    expiry_year = Column(Integer, nullable=False)

    # Card holder info
    cardholder_name = Column(String(100), nullable=False)

    # Status and settings
    is_default = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)

    # Provider info
    provider = Column(String(50), nullable=False)  # payme, click, uzcard
    provider_card_id = Column(String(255), nullable=True)

    # Security
    fingerprint = Column(String(100), nullable=True)  # Unique card fingerprint

    # Usage tracking
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    usage_count = Column(Integer, default=0)

    # Payme verification tracking (for SMS OTP flow)
    verification_attempts = Column(Integer, default=0)
    verification_code_sent_at = Column(DateTime(timezone=True), nullable=True)
    verification_expires_at = Column(DateTime(timezone=True), nullable=True)
    masked_phone = Column(String(20), nullable=True)  # e.g., "99890*****31"
    payme_recurrent = Column(Boolean, default=False)  # From cards.create response

    user = relationship("User", backref="credit_cards")

    def is_expired(self):
        """Check if card is expired"""
        from datetime import datetime, UTC

        now = datetime.now(UTC)
        current_month = now.month
        current_year = now.year

        return (self.expiry_year < current_year) or (
            self.expiry_year == current_year and self.expiry_month < current_month
        )

    def is_verification_expired(self) -> bool:
        """Check if the verification code has expired"""
        if not self.verification_expires_at:
            return True
        return datetime.now(UTC) > self.verification_expires_at

    def can_retry_verification(self) -> bool:
        """Check if more verification attempts are allowed (max 3)"""
        return self.verification_attempts < 3

    def increment_verification_attempts(self) -> int:
        """Increment attempts and return remaining"""
        self.verification_attempts += 1
        return max(0, 3 - self.verification_attempts)

    def reset_verification_state(self):
        """Reset verification state for new code request"""
        self.verification_attempts = 0
        self.verification_code_sent_at = None
        self.verification_expires_at = None
        self.masked_phone = None

    def set_verification_sent(self, masked_phone: str, wait_ms: int):
        """Set verification sent state with expiry time"""
        now = datetime.now(UTC)
        self.verification_code_sent_at = now
        self.verification_expires_at = now + timedelta(milliseconds=wait_ms)
        self.masked_phone = masked_phone
        self.verification_attempts = 0  # Reset attempts on new code

    def mask_card_number(self):
        """Return masked card number"""
        return f"****-****-****-{self.last_four_digits}"

    def to_dict(self, include_verification_state: bool = False):
        data = {
            "id": self.id,
            "card_brand": self.card_brand,
            "last_four_digits": self.last_four_digits,
            "masked_number": self.mask_card_number(),
            "expiry_month": self.expiry_month,
            "expiry_year": self.expiry_year,
            "cardholder_name": self.cardholder_name,
            "is_default": self.is_default,
            "is_active": self.is_active,
            "is_verified": self.is_verified,
            "is_expired": self.is_expired(),
            "provider": self.provider,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

        if include_verification_state:
            data.update(
                {
                    "verification_attempts": self.verification_attempts,
                    "attempts_remaining": max(0, 3 - self.verification_attempts),
                    "can_retry": self.can_retry_verification(),
                    "verification_expired": self.is_verification_expired(),
                    "masked_phone": self.masked_phone,
                }
            )

        return data
