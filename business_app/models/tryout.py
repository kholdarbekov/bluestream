from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship, backref

from business_app import db
from business_app.models import TimestampMixin
from shared.enums import (
    TryoutBottleLedgerEventType,
    TryoutOutcome,
    TryoutStatus,
    TryoutTaskStatus,
    TryoutTaskType,
)


class TrialContact(db.Model, TimestampMixin):
    __tablename__ = "trial_contacts"
    __table_args__ = (
        Index("idx_trial_contacts_phone", "phone"),
        Index("idx_trial_contacts_company", "company_name"),
    )

    id = Column(Integer, primary_key=True)
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=True)
    phone = Column(String(20), nullable=False, index=True)
    company_name = Column(String(200), nullable=True)
    preferred_language = Column(String(5), nullable=False, default="uz")
    notes = Column(Text, nullable=True)

    addresses = relationship(
        "TrialContactAddress",
        back_populates="trial_contact",
        cascade="all, delete-orphan",
    )
    tryouts = relationship(
        "ProductTryout",
        back_populates="trial_contact",
        cascade="all, delete-orphan",
    )

    @property
    def full_name(self) -> str:
        parts = [part for part in [self.first_name, self.last_name] if part]
        return " ".join(parts)

    def to_dict(self):
        return {
            "id": self.id,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "full_name": self.full_name,
            "phone": self.phone,
            "company_name": self.company_name,
            "preferred_language": self.preferred_language,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TrialContactAddress(db.Model, TimestampMixin):
    __tablename__ = "trial_contact_addresses"
    __table_args__ = (Index("idx_trial_contact_addresses_contact", "trial_contact_id"),)

    id = Column(Integer, primary_key=True)
    trial_contact_id = Column(Integer, ForeignKey("trial_contacts.id"), nullable=False, index=True)
    label = Column(String(100), nullable=True)
    full_address = Column(Text, nullable=False)
    district = Column(String(100), nullable=True)
    city = Column(String(100), nullable=True, default="Tashkent")
    latitude = Column(Numeric(10, 7), nullable=True)
    longitude = Column(Numeric(10, 7), nullable=True)
    delivery_notes = Column(Text, nullable=True)
    is_default = Column(Boolean, nullable=False, default=False)

    trial_contact = relationship("TrialContact", back_populates="addresses")

    def to_dict(self):
        return {
            "id": self.id,
            "trial_contact_id": self.trial_contact_id,
            "label": self.label,
            "full_address": self.full_address,
            "district": self.district,
            "city": self.city,
            "latitude": float(self.latitude) if self.latitude is not None else None,
            "longitude": float(self.longitude) if self.longitude is not None else None,
            "delivery_notes": self.delivery_notes,
            "is_default": self.is_default,
        }


class ProductTryout(db.Model, TimestampMixin):
    __tablename__ = "product_tryouts"
    __table_args__ = (
        Index("idx_product_tryouts_status_due", "status", "return_due_at"),
        Index("idx_product_tryouts_contact", "trial_contact_id"),
        UniqueConstraint("tryout_number", name="uq_product_tryouts_number"),
    )

    id = Column(Integer, primary_key=True)
    tryout_number = Column(String(50), nullable=True, unique=True, index=True)
    trial_contact_id = Column(Integer, ForeignKey("trial_contacts.id"), nullable=False, index=True)
    converted_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    status = Column(
        SqlEnum(
            TryoutStatus,
            name="tryout_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=TryoutStatus.DRAFT,
        index=True,
    )
    outcome = Column(
        SqlEnum(
            TryoutOutcome,
            name="tryout_outcome",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=TryoutOutcome.PENDING,
        index=True,
    )
    source = Column(String(20), nullable=False, default="admin", index=True)
    notes = Column(Text, nullable=True)
    internal_notes = Column(Text, nullable=True)
    address_snapshot = Column(JSON, nullable=False, default=dict)
    handoff_completed_at = Column(DateTime(timezone=True), nullable=True)
    return_due_at = Column(DateTime(timezone=True), nullable=True, index=True)
    converted_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)

    trial_contact = relationship("TrialContact", back_populates="tryouts")
    converted_user = relationship("User", foreign_keys=[converted_user_id])
    created_by_user = relationship("User", foreign_keys=[created_by_user_id])
    items = relationship("ProductTryoutItem", back_populates="tryout", cascade="all, delete-orphan")
    tasks = relationship("TryoutTask", back_populates="tryout", cascade="all, delete-orphan")
    bottle_ledger_entries = relationship(
        "TryoutBottleLedger",
        back_populates="tryout",
        cascade="all, delete-orphan",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "tryout_number": self.tryout_number,
            "trial_contact_id": self.trial_contact_id,
            "converted_user_id": self.converted_user_id,
            "created_by_user_id": self.created_by_user_id,
            "status": self.status.value if hasattr(self.status, "value") else self.status,
            "outcome": self.outcome.value if hasattr(self.outcome, "value") else self.outcome,
            "source": self.source,
            "notes": self.notes,
            "internal_notes": self.internal_notes,
            "address_snapshot": self.address_snapshot or {},
            "handoff_completed_at": self.handoff_completed_at.isoformat() if self.handoff_completed_at else None,
            "return_due_at": self.return_due_at.isoformat() if self.return_due_at else None,
            "converted_at": self.converted_at.isoformat() if self.converted_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ProductTryoutItem(db.Model, TimestampMixin):
    __tablename__ = "product_tryout_items"
    __table_args__ = (
        Index("idx_product_tryout_items_tryout", "tryout_id"),
        Index("idx_product_tryout_items_product", "product_id"),
    )

    id = Column(Integer, primary_key=True)
    tryout_id = Column(Integer, ForeignKey("product_tryouts.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    unit_price_snapshot = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"))
    returnable_bottles_due = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"))

    tryout = relationship("ProductTryout", back_populates="items")
    product = relationship("Product", backref=backref("product_tryout_items", lazy="dynamic"))
    bottle_ledger_entries = relationship("TryoutBottleLedger", back_populates="tryout_item")

    def to_dict(self):
        return {
            "id": self.id,
            "tryout_id": self.tryout_id,
            "product_id": self.product_id,
            "product_name": self.product.name if self.product else None,
            "quantity": self.quantity,
            "unit_price_snapshot": float(self.unit_price_snapshot or 0),
            "returnable_bottles_due": float(self.returnable_bottles_due or 0),
        }


class TryoutTask(db.Model, TimestampMixin):
    __tablename__ = "tryout_tasks"
    __table_args__ = (
        Index("idx_tryout_tasks_tryout_status", "tryout_id", "status"),
        Index("idx_tryout_tasks_driver_status", "assigned_driver_user_id", "status"),
    )

    id = Column(Integer, primary_key=True)
    tryout_id = Column(Integer, ForeignKey("product_tryouts.id"), nullable=False, index=True)
    task_type = Column(
        SqlEnum(
            TryoutTaskType,
            name="tryout_task_type",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        index=True,
    )
    status = Column(
        SqlEnum(
            TryoutTaskStatus,
            name="tryout_task_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=TryoutTaskStatus.OPEN,
        index=True,
    )
    assigned_driver_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    completed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    due_at = Column(DateTime(timezone=True), nullable=True, index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    notes = Column(Text, nullable=True)
    completion_payload = Column(JSON, nullable=False, default=dict)

    tryout = relationship("ProductTryout", back_populates="tasks")
    assigned_driver = relationship("User", foreign_keys=[assigned_driver_user_id])
    created_by_user = relationship("User", foreign_keys=[created_by_user_id])
    completed_by_user = relationship("User", foreign_keys=[completed_by_user_id])
    bottle_ledger_entries = relationship("TryoutBottleLedger", back_populates="task")

    def to_dict(self):
        return {
            "id": self.id,
            "tryout_id": self.tryout_id,
            "task_type": self.task_type.value if hasattr(self.task_type, "value") else self.task_type,
            "status": self.status.value if hasattr(self.status, "value") else self.status,
            "assigned_driver_user_id": self.assigned_driver_user_id,
            "created_by_user_id": self.created_by_user_id,
            "completed_by_user_id": self.completed_by_user_id,
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "notes": self.notes,
            "completion_payload": self.completion_payload or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TryoutBottleLedger(db.Model, TimestampMixin):
    __tablename__ = "tryout_bottle_ledger"
    __table_args__ = (
        Index("idx_tryout_bottle_ledger_tryout_created", "tryout_id", "created_at"),
        Index("idx_tryout_bottle_ledger_task_event", "task_id", "event_type"),
        Index("idx_tryout_bottle_ledger_product_event", "product_id", "event_type"),
        UniqueConstraint("idempotency_key", name="uq_tryout_bottle_ledger_idempotency"),
    )

    id = Column(Integer, primary_key=True)
    tryout_id = Column(Integer, ForeignKey("product_tryouts.id"), nullable=False, index=True)
    tryout_item_id = Column(Integer, ForeignKey("product_tryout_items.id"), nullable=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey("tryout_tasks.id"), nullable=True, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    event_type = Column(
        SqlEnum(
            TryoutBottleLedgerEventType,
            name="tryout_bottle_ledger_event_type",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        index=True,
    )
    units = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"))
    occurred_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    notes = Column(Text, nullable=True)
    idempotency_key = Column(String(255), nullable=True)
    entry_metadata = Column(JSON, nullable=False, default=dict)

    tryout = relationship("ProductTryout", back_populates="bottle_ledger_entries")
    tryout_item = relationship("ProductTryoutItem", back_populates="bottle_ledger_entries")
    product = relationship("Product", backref=backref("tryout_bottle_ledger_entries", lazy="dynamic"))
    task = relationship("TryoutTask", back_populates="bottle_ledger_entries")
    actor_user = relationship("User", foreign_keys=[actor_user_id])

    def to_dict(self):
        return {
            "id": self.id,
            "tryout_id": self.tryout_id,
            "tryout_item_id": self.tryout_item_id,
            "product_id": self.product_id,
            "product_name": self.product.name if self.product else None,
            "task_id": self.task_id,
            "actor_user_id": self.actor_user_id,
            "event_type": self.event_type.value if hasattr(self.event_type, "value") else self.event_type,
            "units": float(self.units or 0),
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "notes": self.notes,
            "idempotency_key": self.idempotency_key,
            "entry_metadata": self.entry_metadata or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
