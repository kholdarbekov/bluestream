from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Column,
    Date,
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
from sqlalchemy.sql import text as sa_text
from sqlalchemy.orm import relationship

from business_app import db
from business_app.models import TimestampMixin
from business_app.utils.constants import (
    BottleFineStatus,
    BottleLedgerEventType,
    DriverBottleSessionStatus,
    DriverBottleTransferStatus,
    DriverSessionMembershipStatus,
)


class BottleBalance(db.Model, TimestampMixin):
    """Materialized balance of returnable bottles per customer per address.

    Positive balance means the customer currently holds that many bottles.
    Updated atomically on every ledger write; can be reconciled from ledger.
    """

    __tablename__ = "bottle_balances"
    __table_args__ = (
        UniqueConstraint("user_id", "address_id", name="uq_bottle_balance_user_address"),
        Index("idx_bottle_balances_user", "user_id"),
        Index("idx_bottle_balances_address", "address_id"),
        Index("idx_bottle_balances_balance", "balance"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    address_id = Column(Integer, ForeignKey("addresses.id"), nullable=False)
    balance = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"))
    last_delivery_at = Column(DateTime(timezone=True), nullable=True)
    last_return_at = Column(DateTime(timezone=True), nullable=True)
    notes = Column(Text, nullable=True)

    user = relationship("User", backref="bottle_balances", foreign_keys=[user_id])
    address = relationship("UserAddress", backref="bottle_balances", foreign_keys=[address_id])

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "address_id": self.address_id,
            "balance": float(self.balance or 0),
            "last_delivery_at": self.last_delivery_at.isoformat() if self.last_delivery_at else None,
            "last_return_at": self.last_return_at.isoformat() if self.last_return_at else None,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class BottleLedger(db.Model, TimestampMixin):
    """Event-sourced audit trail for all returnable bottle movements.

    Every bottle movement creates an immutable ledger entry with a signed quantity:
      +  bottles going TO the customer (delivery, initial balance, adjustment up)
      -  bottles coming BACK from the customer (return, collection, adjustment down)

    ``balance_after`` stores the running balance after this event for quick reads.
    ``idempotency_key`` prevents double-counting (e.g. 'delivery:{order_id}').
    """

    __tablename__ = "bottle_ledger"
    __table_args__ = (
        Index("idx_bottle_ledger_user_created", "user_id", "created_at"),
        Index("idx_bottle_ledger_address_created", "address_id", "created_at"),
        Index("idx_bottle_ledger_order", "order_id"),
        Index("idx_bottle_ledger_event_type", "event_type"),
        UniqueConstraint("idempotency_key", name="uq_bottle_ledger_idempotency"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    address_id = Column(Integer, ForeignKey("addresses.id"), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True, index=True)
    delivery_id = Column(Integer, ForeignKey("deliveries.id"), nullable=True, index=True)

    event_type = Column(
        SqlEnum(
            BottleLedgerEventType,
            name="bottle_ledger_event_type",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        index=True,
    )

    quantity = Column(Numeric(precision=12, scale=2), nullable=False)
    balance_after = Column(Numeric(precision=12, scale=2), nullable=False)

    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    occurred_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    notes = Column(Text, nullable=True)
    idempotency_key = Column(String(255), nullable=True)
    entry_metadata = Column(JSON, nullable=False, default=dict)

    user = relationship("User", foreign_keys=[user_id])
    address = relationship("UserAddress", foreign_keys=[address_id])
    order = relationship("Order", foreign_keys=[order_id])
    delivery = relationship("Delivery", foreign_keys=[delivery_id])
    actor_user = relationship("User", foreign_keys=[actor_user_id])

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "address_id": self.address_id,
            "order_id": self.order_id,
            "delivery_id": self.delivery_id,
            "event_type": self.event_type.value if hasattr(self.event_type, "value") else self.event_type,
            "quantity": float(self.quantity or 0),
            "balance_after": float(self.balance_after or 0),
            "actor_user_id": self.actor_user_id,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "notes": self.notes,
            "idempotency_key": self.idempotency_key,
            "entry_metadata": self.entry_metadata or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class BottleFine(db.Model, TimestampMixin):
    """Manual fine record for lost/unreturned bottles.

    Fines are always created manually by admins or drivers — there are no
    automatic triggers or thresholds.  The fine amount is entered per-case;
    some customers may be allowed extra bottles as reserve while others are
    fined immediately.
    """

    __tablename__ = "bottle_fines"
    __table_args__ = (
        Index("idx_bottle_fines_user_status", "user_id", "status"),
        Index("idx_bottle_fines_balance", "bottle_balance_id"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    bottle_balance_id = Column(Integer, ForeignKey("bottle_balances.id"), nullable=False)
    quantity = Column(Numeric(precision=12, scale=2), nullable=False)
    fine_amount = Column(Numeric(precision=10, scale=2), nullable=False)
    status = Column(
        SqlEnum(
            BottleFineStatus,
            name="bottle_fine_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=BottleFineStatus.PENDING,
    )
    issued_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    issued_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    paid_at = Column(DateTime(timezone=True), nullable=True)
    waived_at = Column(DateTime(timezone=True), nullable=True)
    waived_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    notes = Column(Text, nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    bottle_balance = relationship("BottleBalance", backref="fines", foreign_keys=[bottle_balance_id])
    issuer = relationship("User", foreign_keys=[issued_by])
    waiver = relationship("User", foreign_keys=[waived_by])

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "bottle_balance_id": self.bottle_balance_id,
            "quantity": float(self.quantity or 0),
            "fine_amount": float(self.fine_amount or 0),
            "status": self.status.value if hasattr(self.status, "value") else self.status,
            "issued_by": self.issued_by,
            "issued_at": self.issued_at.isoformat() if self.issued_at else None,
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
            "waived_at": self.waived_at.isoformat() if self.waived_at else None,
            "waived_by": self.waived_by,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class DriverBottleSession(db.Model, TimestampMixin):
    """Single trip session: WH load → deliveries → WH return.

    Replaces per-day DriverBottleLoad with per-trip granularity.
    A driver may have at most ONE OPEN session at a time — enforced via a
    partial unique index and validated in the service layer.

    Discrepancy formula (computed on close):
        loaded + transferred_in - delivered + collected - transferred_out - returned_to_wh
    Zero = perfect accountability.  Positive = bottles unaccounted for.
    """

    __tablename__ = "driver_bottle_sessions"
    __table_args__ = (
        Index("idx_dbs_driver_status", "driver_user_id", "status"),
        Index("idx_dbs_driver_started", "driver_user_id", "started_at"),
        Index("idx_dbs_status_started", "status", "started_at"),
        # Partial unique index: at most one OPEN session per driver.
        # The DB-level constraint prevents race conditions on concurrent opens.
        Index(
            "uq_dbs_driver_open",
            "driver_user_id",
            unique=True,
            postgresql_where=sa_text("status = 'open'"),
        ),
    )

    id = Column(Integer, primary_key=True)
    session_ref = Column(String(100), unique=True, nullable=False, index=True)
    driver_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    status = Column(
        SqlEnum(
            DriverBottleSessionStatus,
            name="driver_bottle_session_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=DriverBottleSessionStatus.OPEN,
        index=True,
    )

    # --- Load side ---
    bottles_loaded = Column(Integer, nullable=False, default=0)
    loaded_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    started_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=sa_text("now()"),
    )

    # --- Auto-tallied by service on each ledger write ---
    bottles_delivered = Column(Integer, nullable=False, default=0)
    bottles_collected_from_customers = Column(Integer, nullable=False, default=0)
    # Deducted when sender initiates a transfer (immediately pessimistic)
    bottles_transferred_out = Column(Integer, nullable=False, default=0)
    # Credited when receiver confirms a transfer
    bottles_transferred_in = Column(Integer, nullable=False, default=0)

    # --- Close side (NULL while OPEN) ---
    bottles_returned_to_warehouse = Column(Integer, nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    closed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    discrepancy = Column(Integer, nullable=True)  # computed on close

    # --- Admin override ---
    force_closed = Column(Boolean, nullable=False, default=False)
    force_close_reason = Column(Text, nullable=True)

    notes = Column(Text, nullable=True)
    session_metadata = Column(JSON, nullable=False, default=dict)

    driver = relationship("User", foreign_keys=[driver_user_id], backref="bottle_sessions")
    loaded_by = relationship("User", foreign_keys=[loaded_by_user_id])
    closed_by = relationship("User", foreign_keys=[closed_by_user_id])

    session_orders = relationship(
        "DriverBottleSessionOrder",
        back_populates="session",
        cascade="all, delete-orphan",
    )
    memberships = relationship(
        "DriverSessionMembership",
        back_populates="session",
        cascade="all, delete-orphan",
    )
    transfers_out = relationship(
        "DriverBottleTransfer",
        foreign_keys="DriverBottleTransfer.sender_session_id",
        back_populates="sender_session",
    )
    transfers_in = relationship(
        "DriverBottleTransfer",
        foreign_keys="DriverBottleTransfer.receiver_session_id",
        back_populates="receiver_session",
    )

    def __init__(self, **kwargs):
        import uuid
        super().__init__(**kwargs)
        if not self.session_ref:
            self.session_ref = str(uuid.uuid4())

    @property
    def current_inventory(self) -> int:
        """Bottles currently on truck (real-time; valid while session is OPEN)."""
        return (
            (self.bottles_loaded or 0)
            + (self.bottles_transferred_in or 0)
            - (self.bottles_delivered or 0)
            + (self.bottles_collected_from_customers or 0)
            - (self.bottles_transferred_out or 0)
        )

    def compute_discrepancy(self) -> None:
        """Compute and store discrepancy on session close."""
        self.discrepancy = (
            (self.bottles_loaded or 0)
            + (self.bottles_transferred_in or 0)
            - (self.bottles_delivered or 0)
            + (self.bottles_collected_from_customers or 0)
            - (self.bottles_transferred_out or 0)
            - (self.bottles_returned_to_warehouse or 0)
        )

    def to_dict(self):
        return {
            "id": self.id,
            "session_ref": self.session_ref,
            "driver_user_id": self.driver_user_id,
            "status": self.status.value if hasattr(self.status, "value") else self.status,
            "bottles_loaded": self.bottles_loaded,
            "bottles_delivered": self.bottles_delivered,
            "bottles_collected_from_customers": self.bottles_collected_from_customers,
            "bottles_transferred_out": self.bottles_transferred_out,
            "bottles_transferred_in": self.bottles_transferred_in,
            "current_inventory": self.current_inventory,
            "bottles_returned_to_warehouse": self.bottles_returned_to_warehouse,
            "discrepancy": self.discrepancy,
            "force_closed": self.force_closed,
            "force_close_reason": self.force_close_reason,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "notes": self.notes,
            "session_metadata": self.session_metadata or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class DriverBottleSessionOrder(db.Model, TimestampMixin):
    """Binds a specific order to a bottle session.

    Created when a driver completes a delivery while a session is open.
    An order belongs to at most one session (enforced by unique constraint).
    """

    __tablename__ = "driver_bottle_session_orders"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_dbso_order"),
        Index("idx_dbso_session", "session_id"),
        Index("idx_dbso_order", "order_id"),
    )

    id = Column(Integer, primary_key=True)
    session_id = Column(
        Integer, ForeignKey("driver_bottle_sessions.id"), nullable=False, index=True
    )
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    # The driver who actually accepted the order — may differ from session owner
    # when co-driver (session member) accepts the order. NULL for legacy records.
    accepted_by_driver_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    added_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    session = relationship("DriverBottleSession", back_populates="session_orders")
    order = relationship("Order", backref="bottle_session_binding")
    accepted_by_driver = relationship("User", foreign_keys=[accepted_by_driver_id])

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "order_id": self.order_id,
            "accepted_by_driver_id": self.accepted_by_driver_id,
            "added_at": self.added_at.isoformat() if self.added_at else None,
        }


class DriverSessionMembership(db.Model, TimestampMixin):
    """Co-driver membership: allows a driver to operate under another driver's open session.

    Use case: two drivers share one truck. Driver A loads bottles and opens a
    session; Driver B joins that session. While ACTIVE, Driver B's order
    acceptances are validated against — and tallied to — Driver A's session.

    Rules:
    - A driver can only have ONE active membership at a time (partial unique index).
    - A driver with their own OPEN session cannot join another session.
    - When the owning session is closed, all active memberships are auto-REVOKED.
    """

    __tablename__ = "driver_session_memberships"
    __table_args__ = (
        Index("idx_dsm_session", "session_id"),
        Index("idx_dsm_member_status", "member_driver_id", "status"),
        Index("idx_dsm_owner_status", "session_owner_id", "status"),
        # At most one ACTIVE membership per driver at any time
        Index(
            "uq_dsm_member_active",
            "member_driver_id",
            unique=True,
            postgresql_where=sa_text("status = 'active'"),
        ),
    )

    id = Column(Integer, primary_key=True)
    session_id = Column(
        Integer, ForeignKey("driver_bottle_sessions.id"), nullable=False, index=True
    )
    # Denormalized for fast "who owns this session" lookups without a join
    session_owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    member_driver_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    status = Column(
        SqlEnum(
            DriverSessionMembershipStatus,
            name="driver_session_membership_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=DriverSessionMembershipStatus.ACTIVE,
        index=True,
    )

    joined_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    left_at = Column(DateTime(timezone=True), nullable=True)
    invited_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    notes = Column(Text, nullable=True)

    session = relationship("DriverBottleSession", back_populates="memberships")
    session_owner = relationship("User", foreign_keys=[session_owner_id])
    member_driver = relationship("User", foreign_keys=[member_driver_id])
    invited_by = relationship("User", foreign_keys=[invited_by_user_id])

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "session_owner_id": self.session_owner_id,
            "member_driver_id": self.member_driver_id,
            "status": self.status.value if hasattr(self.status, "value") else self.status,
            "joined_at": self.joined_at.isoformat() if self.joined_at else None,
            "left_at": self.left_at.isoformat() if self.left_at else None,
            "invited_by_user_id": self.invited_by_user_id,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class DriverBottleTransfer(db.Model, TimestampMixin):
    """Mid-route driver-to-driver bottle transfer.

    Sender submits the transfer (deducts from their session immediately).
    Receiver confirms the quantity (credits their session).
    If quantities differ, a dispute is filed for admin resolution.

    receiver_session_id is nullable: the receiver may not yet have an open
    session when the sender initiates the transfer. It is populated when the
    receiver confirms.
    """

    __tablename__ = "driver_bottle_transfers"
    __table_args__ = (
        Index("idx_dbt_sender_session", "sender_session_id"),
        Index("idx_dbt_receiver_session", "receiver_session_id"),
        Index("idx_dbt_receiver_driver", "receiver_driver_id"),
        Index("idx_dbt_status_created", "status", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    transfer_ref = Column(String(100), unique=True, nullable=False, index=True)

    sender_session_id = Column(
        Integer, ForeignKey("driver_bottle_sessions.id"), nullable=False, index=True
    )
    sender_driver_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    receiver_driver_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Populated when the receiver confirms (they may not have a session yet at send time)
    receiver_session_id = Column(
        Integer, ForeignKey("driver_bottle_sessions.id"), nullable=True, index=True
    )

    declared_quantity = Column(Integer, nullable=False)
    confirmed_quantity = Column(Integer, nullable=True)  # NULL until receiver confirms

    status = Column(
        SqlEnum(
            DriverBottleTransferStatus,
            name="driver_bottle_transfer_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=DriverBottleTransferStatus.PENDING,
        index=True,
    )

    sent_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolved_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    dispute_notes = Column(Text, nullable=True)
    resolution_notes = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)

    sender_session = relationship(
        "DriverBottleSession",
        foreign_keys=[sender_session_id],
        back_populates="transfers_out",
    )
    receiver_session = relationship(
        "DriverBottleSession",
        foreign_keys=[receiver_session_id],
        back_populates="transfers_in",
    )
    sender_driver = relationship("User", foreign_keys=[sender_driver_id])
    receiver_driver = relationship("User", foreign_keys=[receiver_driver_id])
    resolved_by = relationship("User", foreign_keys=[resolved_by_user_id])

    def __init__(self, **kwargs):
        import uuid
        super().__init__(**kwargs)
        if not self.transfer_ref:
            self.transfer_ref = str(uuid.uuid4())

    def to_dict(self):
        return {
            "id": self.id,
            "transfer_ref": self.transfer_ref,
            "sender_session_id": self.sender_session_id,
            "sender_driver_id": self.sender_driver_id,
            "receiver_driver_id": self.receiver_driver_id,
            "receiver_session_id": self.receiver_session_id,
            "declared_quantity": self.declared_quantity,
            "confirmed_quantity": self.confirmed_quantity,
            "status": self.status.value if hasattr(self.status, "value") else self.status,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by_user_id": self.resolved_by_user_id,
            "dispute_notes": self.dispute_notes,
            "resolution_notes": self.resolution_notes,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
