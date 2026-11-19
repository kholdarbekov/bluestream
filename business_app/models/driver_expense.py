"""A driver's claim for cash spent from the money he holds on a trip (spec 2026-11-17).

Every CHECK is named and mirrors the frozen copy in migration 20261118_1200_driver_cash_expenses; the
vocabulary ones are built with `_quoted` over the `shared/staff_constants.py` tuples. Who may write which
row, and when, is guarded by the expense service under the row lock (expense row first, session row last).
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from business_app import db
from business_app.models.base import TimestampMixin
from business_app.models.sales import _quoted
from shared.staff_constants import (
    DRIVER_EXPENSE_CATEGORIES,
    DRIVER_EXPENSE_NOTE_REQUIRED_CATEGORIES,
    DRIVER_EXPENSE_STATUSES,
)

_TABLE = "driver_cash_expenses"


class DriverCashExpense(db.Model, TimestampMixin):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"

    NOTE_MAX_LENGTH = 500
    REASON_MIN_LENGTH = 5  # as for returns; the decision writes refuse below it
    REASON_MAX_LENGTH = 500
    IDEMPOTENCY_KEY_MAX_LENGTH = 64
    RECEIPT_FILE_ID_MAX_LENGTH = 255

    __tablename__ = _TABLE
    __table_args__ = (
        CheckConstraint("amount > 0", name=f"ck_{_TABLE}_amount_positive"),
        CheckConstraint(f"category IN ({_quoted(DRIVER_EXPENSE_CATEGORIES)})", name=f"ck_{_TABLE}_category"),
        CheckConstraint(
            f"category NOT IN ({_quoted(DRIVER_EXPENSE_NOTE_REQUIRED_CATEGORIES)}) "
            "OR (note IS NOT NULL AND length(trim(note)) > 0)",
            name=f"ck_{_TABLE}_note_required",
        ),
        CheckConstraint(f"status IN ({_quoted(DRIVER_EXPENSE_STATUSES)})", name=f"ck_{_TABLE}_status"),
        CheckConstraint(
            "(status = 'approved' AND approved_amount IS NOT NULL AND approved_amount > 0 "
            "AND approved_amount <= amount) "
            "OR (status <> 'approved' AND approved_amount IS NULL)",
            name=f"ck_{_TABLE}_approved_amount",
        ),
        CheckConstraint(
            "(status IN ('approved', 'rejected') AND decided_by_user_id IS NOT NULL AND decided_at IS NOT NULL) "
            "OR (status NOT IN ('approved', 'rejected') AND decided_by_user_id IS NULL AND decided_at IS NULL)",
            name=f"ck_{_TABLE}_decided",
        ),
        CheckConstraint(
            "status <> 'rejected' OR (decision_reason IS NOT NULL AND length(trim(decision_reason)) > 0)",
            name=f"ck_{_TABLE}_rejected_reason",
        ),
        CheckConstraint(
            "(status = 'withdrawn' AND withdrawn_at IS NOT NULL) "
            "OR (status <> 'withdrawn' AND withdrawn_at IS NULL)",
            name=f"ck_{_TABLE}_withdrawn",
        ),
        CheckConstraint(
            "status <> 'pending' OR (approved_amount IS NULL AND decided_by_user_id IS NULL "
            "AND decided_at IS NULL AND decision_reason IS NULL AND withdrawn_at IS NULL "
            "AND NOT self_decided)",
            name=f"ck_{_TABLE}_pending_clean",
        ),
        UniqueConstraint("driver_user_id", "idempotency_key", name=f"uq_{_TABLE}_driver_idempotency_key"),
        Index(f"ix_{_TABLE}_status_submitted", "status", "submitted_at"),
        Index(f"ix_{_TABLE}_session_status", "driver_cash_session_id", "status"),
        Index(f"ix_{_TABLE}_driver_status", "driver_user_id", "status"),
    )

    id = Column(Integer, primary_key=True)
    driver_user_id = Column(Integer, ForeignKey("users.id", name=f"fk_{_TABLE}_driver_user_id"), nullable=False)
    driver_cash_session_id = Column(
        Integer, ForeignKey("driver_cash_sessions.id", name=f"fk_{_TABLE}_driver_cash_session_id"), nullable=False
    )
    category = Column(String(32), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    note = Column(String(NOTE_MAX_LENGTH))
    # Telegram's file reference (as for support attachments and visit photos); a claim without one is valid.
    receipt_file_id = Column(String(RECEIPT_FILE_ID_MAX_LENGTH))
    latitude = Column(Float)
    longitude = Column(Float)
    status = Column(String(16), nullable=False, default=PENDING)
    idempotency_key = Column(String(IDEMPOTENCY_KEY_MAX_LENGTH), nullable=False)
    approved_amount = Column(Numeric(12, 2))
    decided_by_user_id = Column(Integer, ForeignKey("users.id", name=f"fk_{_TABLE}_decided_by_user_id"))
    decided_at = Column(DateTime(timezone=True))
    decision_reason = Column(String(REASON_MAX_LENGTH))
    self_decided = Column(Boolean, nullable=False, default=False)
    submitted_at = Column(DateTime(timezone=True), nullable=False)
    withdrawn_at = Column(DateTime(timezone=True))

    session = relationship("DriverCashSession", foreign_keys=[driver_cash_session_id])
    driver_user = relationship("User", foreign_keys=[driver_user_id])
    decided_by = relationship("User", foreign_keys=[decided_by_user_id])
