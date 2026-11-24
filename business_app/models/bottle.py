from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
    and_,
    exists,
    or_,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.sql import text as sa_text
from sqlalchemy.orm import relationship

from business_app import db
from business_app.models import TimestampMixin
from shared.enums import (
    BottleFineStatus,
    BottleLedgerEventType,
    DriverBottleSessionStatus,
    DriverBottleTransferStatus,
    DriverSessionMembershipStatus,
)


class BottleBalance(db.Model, TimestampMixin):
    """Materialized balance of returnable bottles for one PHYSICAL PLACE.

    A place is the address group when the address is grouped, else the address
    itself (spec 2026-07-27 section 3). Positive balance means that many bottles
    are currently held at the place. Updated atomically on every ledger write;
    reconcilable from the ledger. There is deliberately no `user_id` — bottles at
    a shared place are one pool, not per-coworker slices.
    """

    __tablename__ = "bottle_balances"
    __table_args__ = (
        # Exactly one scope key. Written as the portable `<>`-of-two-IS-NULLs
        # rather than Postgres' num_nonnulls(), which SQLite does not implement
        # and which would break db.create_all() in the test suite.
        CheckConstraint(
            "(address_group_id IS NULL) <> (address_id IS NULL)",
            name="ck_bottle_balance_scope",
        ),
        UniqueConstraint("address_group_id", name="uq_bottle_balance_group"),
        UniqueConstraint("address_id", name="uq_bottle_balance_addr"),
        Index("idx_bottle_balances_group", "address_group_id"),
        Index("idx_bottle_balances_address", "address_id"),
        Index("idx_bottle_balances_balance", "balance"),
    )

    id = Column(Integer, primary_key=True)
    address_group_id = Column(Integer, ForeignKey("address_groups.id"), nullable=True)
    address_id = Column(Integer, ForeignKey("addresses.id"), nullable=True)
    balance = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"))
    last_delivery_at = Column(DateTime(timezone=True), nullable=True)
    last_return_at = Column(DateTime(timezone=True), nullable=True)
    notes = Column(Text, nullable=True)

    address = relationship("UserAddress", backref="bottle_balances", foreign_keys=[address_id])
    address_group = relationship("AddressGroup", foreign_keys=[address_group_id])

    def to_dict(self):
        return {
            "id": self.id,
            "address_group_id": self.address_group_id,
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
        Index("idx_bottle_ledger_group_occurred", "address_group_id", "occurred_at"),
        UniqueConstraint("idempotency_key", name="uq_bottle_ledger_idempotency"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    address_id = Column(Integer, ForeignKey("addresses.id"), nullable=False, index=True)
    address_group_id = Column(Integer, ForeignKey("address_groups.id"), nullable=True, index=True)
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
    occurred_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
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
            "address_group_id": self.address_group_id,
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
        Index("idx_bottle_fines_address", "address_id"),
        UniqueConstraint("idempotency_key", name="uq_bottle_fines_idempotency_key"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    address_id = Column(Integer, ForeignKey("addresses.id"), nullable=False)
    # The scope AT ISSUE, frozen. Without it a fine issued while an address was
    # grouped puts FINE_ISSUED in the place ledger, and — if the address later
    # leaves — FINE_PAID in the address ledger, splitting the pair across two
    # ledgers and leaving both balances wrong.
    address_group_id = Column(Integer, ForeignKey("address_groups.id"), nullable=True)
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
    issued_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    paid_at = Column(DateTime(timezone=True), nullable=True)
    waived_at = Column(DateTime(timezone=True), nullable=True)
    waived_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    notes = Column(Text, nullable=True)
    # Client-supplied retry token, NAMESPACED SERVER-SIDE — see
    # `BottleTrackingService.compose_client_idempotency_key`. Same shape as
    # `bottle_ledger.idempotency_key` (:128): String(255), nullable, plain
    # global UNIQUE. NULL for every server-initiated and legacy-client fine;
    # Postgres AND SQLite both treat NULLs as distinct under a plain UNIQUE, so
    # un-keyed fines stay unconstrained on both engines.
    #
    # The guard has to live on THIS table, not on the FINE_ISSUED ledger row:
    # that row carries `quantity=Decimal("0")`, so the fine's money lives here
    # and keying only the ledger would let the money duplicate while its audit
    # trail deduped.
    idempotency_key = Column(String(255), nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    address = relationship("UserAddress", foreign_keys=[address_id])
    issuer = relationship("User", foreign_keys=[issued_by])
    waiver = relationship("User", foreign_keys=[waived_by])

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "address_id": self.address_id,
            "address_group_id": self.address_group_id,
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
        loaded + transferred_in - delivered - transferred_out
        + collected + empties_in - empties_out - returned_to_wh
    Zero = perfect accountability.  Positive = bottles unaccounted for.
    """

    __tablename__ = "driver_bottle_sessions"
    __table_args__ = (
        Index("idx_dbs_driver_status", "driver_user_id", "status"),
        Index("idx_dbs_driver_started", "driver_user_id", "started_at"),
        Index("idx_dbs_status_started", "status", "started_at"),
        # Partial unique index: at most one OPEN session per driver.
        # The DB-level constraint prevents race conditions on concurrent opens.
        # ``sqlite_where`` mirrors postgresql_where so test SQLite honours the
        # partial filter — otherwise SQLite drops the WHERE clause and the
        # index becomes a FULL unique on driver_user_id, blocking the seed
        # of any second session per driver (even when one is closed).
        Index(
            "uq_dbs_driver_open",
            "driver_user_id",
            unique=True,
            postgresql_where=sa_text("status = 'open'"),
            sqlite_where=sa_text("status = 'open'"),
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
    # Empties handed to / received from another driver. The two counters above move FULL bottles only; empties
    # collected from customers are `bottles_collected_from_customers`.
    empties_transferred_out = Column(Integer, nullable=False, default=0, server_default=sa_text("0"))
    empties_transferred_in = Column(Integer, nullable=False, default=0, server_default=sa_text("0"))

    # --- Close side (NULL while OPEN) ---
    bottles_returned_to_warehouse = Column(Integer, nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    closed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    discrepancy = Column(Integer, nullable=True)  # computed on close

    # --- Admin override ---
    force_closed = Column(Boolean, nullable=False, default=False)
    force_close_reason = Column(Text, nullable=True)

    # --- Reopen audit (admin retroactively edits a delivered order) ---
    # When a closed session must be adjusted, status transitions CLOSED/FORCE_CLOSED → OPEN
    # so ledger writes for the order edit re-tally cleanly. The driver re-closes
    # the session afterward; reopen_count tracks how many times this has happened.
    reopened_at = Column(DateTime(timezone=True), nullable=True)
    reopened_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    reopened_reason = Column(Text, nullable=True)
    reopen_count = Column(Integer, nullable=False, default=0, server_default=sa_text("0"))

    notes = Column(Text, nullable=True)
    session_metadata = Column(JSON, nullable=False, default=dict)

    driver = relationship("User", foreign_keys=[driver_user_id], backref="bottle_sessions")
    loaded_by = relationship("User", foreign_keys=[loaded_by_user_id])
    closed_by = relationship("User", foreign_keys=[closed_by_user_id])
    reopened_by = relationship("User", foreign_keys=[reopened_by_user_id])

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

    unit_lines = relationship(
        "DriverBottleSessionUnit",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="DriverBottleSessionUnit.product_id",
    )

    def __init__(self, **kwargs):
        import uuid

        super().__init__(**kwargs)
        if not self.session_ref:
            self.session_ref = str(uuid.uuid4())

    # The columns `full_bottles_on_board` reads. The progress guard expires them under its crew lock, so the count
    # reloads them (BottleTrackingService._lock_crew_delivery_persons).
    FULL_ON_BOARD_TALLIES = ("bottles_loaded", "bottles_transferred_in", "bottles_delivered", "bottles_transferred_out")

    @property
    def full_bottles_on_board(self) -> int:
        """Full bottles on the truck: loaded + transferred in - delivered - transferred out.

        The base term `current_inventory` and `compute_discrepancy` share. Unclamped, so a session whose tallies
        disagree (more delivered than loaded) keeps its true sign in both. The ETA plan clamps it at 0 (R6).
        """
        return (
            (self.bottles_loaded or 0)
            + (self.bottles_transferred_in or 0)
            - (self.bottles_delivered or 0)
            - (self.bottles_transferred_out or 0)
        )

    @property
    def empties_on_board(self) -> int:
        """Empties on the truck: collected from customers + received from colleagues - handed to colleagues."""
        return (
            (self.bottles_collected_from_customers or 0)
            + (self.empties_transferred_in or 0)
            - (self.empties_transferred_out or 0)
        )

    @property
    def current_inventory(self) -> int:
        """Bottles currently on truck (real-time; valid while session is OPEN): the full ones and the empties."""
        return self.full_bottles_on_board + self.empties_on_board

    @property
    def transferable_full(self) -> int:
        """Full bottles the driver can hand over: never negative, whatever a legacy tally says."""
        return max(self.full_bottles_on_board, 0)

    @property
    def transferable_empties(self) -> int:
        """Empties the driver can hand over: never negative."""
        return max(self.empties_on_board, 0)

    @property
    def transferable_any(self) -> bool:
        """Anything to hand over: full bottles, empties or any one-way unit line."""
        return bool(
            self.transferable_full or self.transferable_empties or any(line.transferable for line in self.unit_lines)
        )

    def compute_discrepancy(self) -> None:
        """Compute and store discrepancy on session close."""
        self.discrepancy = self.current_inventory - (self.bottles_returned_to_warehouse or 0)

    @hybrid_property
    def has_discrepancy(self) -> bool:
        """THE answer to "does this trip have a discrepancy": its 18.9 L one, or any line's, is set and not 0.

        Published by the session payload (the admin page's red row reads it); the admin list's "only discrepancies"
        filter is the SQL twin below. Nothing else decides it.
        """
        values = (self.discrepancy, *(line.discrepancy for line in self.unit_lines))
        return any(value not in (None, 0) for value in values)

    @has_discrepancy.inplace.expression
    @classmethod
    def _has_discrepancy_expression(cls):
        return or_(
            and_(cls.discrepancy.isnot(None), cls.discrepancy != 0),
            exists().where(
                DriverBottleSessionUnit.session_id == cls.id,
                DriverBottleSessionUnit.discrepancy.isnot(None),
                DriverBottleSessionUnit.discrepancy != 0,
            ),
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
            "empties_transferred_out": self.empties_transferred_out,
            "empties_transferred_in": self.empties_transferred_in,
            "full_bottles_on_board": self.full_bottles_on_board,
            "empties_on_board": self.empties_on_board,
            "transferable_full": self.transferable_full,
            "transferable_empties": self.transferable_empties,
            "current_inventory": self.current_inventory,
            "bottles_returned_to_warehouse": self.bottles_returned_to_warehouse,
            "discrepancy": self.discrepancy,
            "force_closed": self.force_closed,
            "force_close_reason": self.force_close_reason,
            "reopened_at": self.reopened_at.isoformat() if self.reopened_at else None,
            "reopened_by_user_id": self.reopened_by_user_id,
            "reopened_reason": self.reopened_reason,
            "reopen_count": self.reopen_count or 0,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "notes": self.notes,
            "session_metadata": self.session_metadata or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class DriverBottleSessionUnit(db.Model):
    """One one-way unit product (no bottle comes back, e.g. 10 L water) on one trip.

    Same tallies as the 18.9 L session columns, one row per (trip, product). Its discrepancy is its own and is
    never added to the session's.
    """

    __tablename__ = "driver_bottle_session_units"
    __table_args__ = (
        UniqueConstraint("session_id", "product_id", name="uq_dbsu_session_product"),
        CheckConstraint(
            "loaded >= 0 AND delivered >= 0 AND transferred_in >= 0 AND transferred_out >= 0 "
            "AND (returned IS NULL OR returned >= 0)",
            name="ck_dbsu_counts_non_negative",
        ),
    )

    # The columns `on_board` reads (the guard expires them under its lock, as for the session tallies).
    ON_BOARD_TALLIES = ("loaded", "transferred_in", "delivered", "transferred_out")

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("driver_bottle_sessions.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    loaded = Column(Integer, nullable=False, default=0, server_default=sa_text("0"))
    delivered = Column(Integer, nullable=False, default=0, server_default=sa_text("0"))
    transferred_in = Column(Integer, nullable=False, default=0, server_default=sa_text("0"))
    transferred_out = Column(Integer, nullable=False, default=0, server_default=sa_text("0"))
    returned = Column(Integer, nullable=True)
    discrepancy = Column(Integer, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=sa_text("now()"),
    )

    session = relationship("DriverBottleSession", back_populates="unit_lines")
    product = relationship("Product")

    @property
    def on_board(self) -> int:
        """loaded + transferred in - delivered - transferred out; unclamped, so a bad tally keeps its sign."""
        return (self.loaded or 0) + (self.transferred_in or 0) - (self.delivered or 0) - (self.transferred_out or 0)

    @property
    def transferable(self) -> int:
        return max(self.on_board, 0)

    def compute_discrepancy(self) -> None:
        self.discrepancy = self.on_board - (self.returned or 0)


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
    session_id = Column(Integer, ForeignKey("driver_bottle_sessions.id"), nullable=False, index=True)
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
    session_id = Column(Integer, ForeignKey("driver_bottle_sessions.id"), nullable=False, index=True)
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

    sender_session_id = Column(Integer, ForeignKey("driver_bottle_sessions.id"), nullable=False, index=True)
    sender_driver_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    receiver_driver_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Populated when the receiver confirms (they may not have a session yet at send time)
    receiver_session_id = Column(Integer, ForeignKey("driver_bottle_sessions.id"), nullable=True, index=True)

    # `declared_quantity` / `confirmed_quantity` are the FULL bottles; the empties travel in the two columns below.
    declared_quantity = Column(Integer, nullable=False)
    confirmed_quantity = Column(Integer, nullable=True)  # NULL until receiver confirms
    declared_empty_quantity = Column(Integer, nullable=False, default=0, server_default=sa_text("0"))
    confirmed_empty_quantity = Column(Integer, nullable=True)  # NULL until receiver confirms

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
    rejected_at = Column(DateTime(timezone=True), nullable=True)

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
    unit_lines = relationship(
        "DriverBottleTransferUnit",
        back_populates="transfer",
        cascade="all, delete-orphan",
        order_by="DriverBottleTransferUnit.product_id",
    )

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
            "declared_empty_quantity": self.declared_empty_quantity,
            "confirmed_empty_quantity": self.confirmed_empty_quantity,
            "status": self.status.value if hasattr(self.status, "value") else self.status,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "rejected_at": self.rejected_at.isoformat() if self.rejected_at else None,
            "resolved_by_user_id": self.resolved_by_user_id,
            "dispute_notes": self.dispute_notes,
            "resolution_notes": self.resolution_notes,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class DriverBottleTransferUnit(db.Model):
    """One one-way unit product on a driver-to-driver transfer: what the sender declared, the receiver counted
    and the admin resolved (NULL until each step happens)."""

    __tablename__ = "driver_bottle_transfer_units"
    __table_args__ = (
        UniqueConstraint("transfer_id", "product_id", name="uq_dbtu_transfer_product"),
        CheckConstraint(
            "declared >= 0 AND (confirmed IS NULL OR confirmed >= 0) AND (resolved IS NULL OR resolved >= 0)",
            name="ck_dbtu_counts_non_negative",
        ),
    )

    id = Column(Integer, primary_key=True)
    transfer_id = Column(Integer, ForeignKey("driver_bottle_transfers.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    declared = Column(Integer, nullable=False)
    confirmed = Column(Integer, nullable=True)
    resolved = Column(Integer, nullable=True)

    transfer = relationship("DriverBottleTransfer", back_populates="unit_lines")
    product = relationship("Product")
