from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    JSON,
)
from sqlalchemy.orm import relationship, backref

from business_app import db
from business_app.models import TimestampMixin
from shared.enums import CorporateContractTrackingMode


class CorporateContractStatus(Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"


class CorporatePrepaymentEventType(Enum):
    TOPUP = "topup"
    RESERVE = "reserve"
    CONSUME = "consume"
    RELEASE = "release"
    ADJUSTMENT = "adjustment"
    # Money-only events used when the contract's tracking_mode == AMOUNT
    # (grocery-store accounts). units/product_id/balance_id are NULL on these rows.
    CHARGE = "charge"
    COLLECT = "collect"


class CorporateContract(db.Model, TimestampMixin):
    __tablename__ = "corporate_contracts"
    __table_args__ = (
        Index("idx_corporate_contracts_user_status", "user_id", "status"),
        Index("idx_corporate_contracts_active", "is_active"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    contract_number = Column(String(100), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False)
    status = Column(
        SqlEnum(
            CorporateContractStatus,
            name="corporate_contract_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=CorporateContractStatus.ACTIVE,
        index=True,
    )
    start_date = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    end_date = Column(DateTime(timezone=True), nullable=True)
    currency = Column(String(3), nullable=False, default="UZS")
    bank_details = Column(JSON, nullable=True, default=dict)
    notes = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    is_loyalty_points_eligible = Column(Boolean, nullable=False, default=False, index=True)
    allows_debt = Column(Boolean, nullable=False, default=False, index=True)
    tracking_mode = Column(
        SqlEnum(
            CorporateContractTrackingMode,
            name="corporate_contract_tracking_mode",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=CorporateContractTrackingMode.UNITS,
        server_default=CorporateContractTrackingMode.UNITS.value,
        index=True,
    )
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    user = relationship(
        "User",
        foreign_keys=[user_id],
        backref=backref("corporate_contracts", lazy="dynamic"),
    )
    created_by_user = relationship("User", foreign_keys=[created_by_user_id])
    updated_by_user = relationship("User", foreign_keys=[updated_by_user_id])
    product_prices = relationship(
        "CorporateContractProductPrice",
        back_populates="contract",
        cascade="all, delete-orphan",
    )
    prepayment_account = relationship(
        "CorporatePrepaymentAccount",
        back_populates="contract",
        uselist=False,
        cascade="all, delete-orphan",
    )
    ledger_entries = relationship(
        "CorporatePrepaymentLedger",
        back_populates="contract",
    )

    @property
    def prepayment_balances(self):
        if not self.prepayment_account:
            return []
        return self.prepayment_account.product_balances

    @property
    def is_currently_active(self) -> bool:
        now = datetime.now(timezone.utc)
        if not self.is_active:
            return False
        if self.status != CorporateContractStatus.ACTIVE:
            return False
        # Normalise stored dates: SQLite returns naive datetimes even for
        # DateTime(timezone=True) columns; assume UTC if tzinfo is absent.
        start = (
            self.start_date.replace(tzinfo=timezone.utc)
            if self.start_date and self.start_date.tzinfo is None
            else self.start_date
        )
        end = (
            self.end_date.replace(tzinfo=timezone.utc)
            if self.end_date and self.end_date.tzinfo is None
            else self.end_date
        )
        if start and start > now:
            return False
        if end and end < now:
            return False
        return True

    @property
    def is_amount_tracked(self) -> bool:
        return self.tracking_mode == CorporateContractTrackingMode.AMOUNT

    @property
    def is_units_tracked(self) -> bool:
        return self.tracking_mode == CorporateContractTrackingMode.UNITS

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "contract_number": self.contract_number,
            "name": self.name,
            "status": self.status.value if hasattr(self.status, "value") else self.status,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "currency": self.currency,
            "bank_details": self.bank_details or {},
            "notes": self.notes,
            "is_active": self.is_active,
            "is_loyalty_points_eligible": self.is_loyalty_points_eligible,
            "allows_debt": self.allows_debt,
            "tracking_mode": self.tracking_mode.value if hasattr(self.tracking_mode, "value") else self.tracking_mode,
            "is_currently_active": self.is_currently_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CorporateContractProductPrice(db.Model, TimestampMixin):
    __tablename__ = "corporate_contract_product_prices"
    __table_args__ = (
        UniqueConstraint(
            "contract_id",
            "product_id",
            name="uq_corporate_contract_product_price",
        ),
        Index(
            "idx_corporate_contract_product_prices_contract",
            "contract_id",
            "is_active",
        ),
    )

    id = Column(Integer, primary_key=True)
    contract_id = Column(Integer, ForeignKey("corporate_contracts.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    unit_price = Column(Numeric(precision=10, scale=2), nullable=False)
    is_prepayment_eligible = Column(Boolean, nullable=False, default=True, index=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    notes = Column(Text, nullable=True)

    contract = relationship("CorporateContract", back_populates="product_prices")
    product = relationship(
        "Product",
        backref=backref("corporate_contract_prices", lazy="dynamic"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "contract_id": self.contract_id,
            "product_id": self.product_id,
            "product_name": getattr(self.product, "name", None),
            "product_sku": getattr(self.product, "sku", None),
            "product_size": getattr(getattr(self.product, "size", None), "value", getattr(self.product, "size", None)),
            "unit_price": float(self.unit_price) if self.unit_price is not None else None,
            "is_prepayment_eligible": self.is_prepayment_eligible,
            "is_active": self.is_active,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CorporatePrepaymentAccount(db.Model, TimestampMixin):
    __tablename__ = "corporate_prepayment_accounts"
    __table_args__ = (
        UniqueConstraint("contract_id", name="uq_corporate_prepayment_account_contract"),
        Index("idx_corporate_prepayment_accounts_contract_active", "contract_id", "is_active"),
    )

    id = Column(Integer, primary_key=True)
    contract_id = Column(Integer, ForeignKey("corporate_contracts.id"), nullable=False, index=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    last_topup_at = Column(DateTime(timezone=True), nullable=True)
    # Money-mode fields. Used only when parent contract.tracking_mode == AMOUNT.
    # Sign convention: outstanding_amount > 0 = customer owes us; < 0 = customer credit.
    outstanding_amount = Column(
        Numeric(precision=14, scale=2),
        nullable=False,
        default=Decimal("0.00"),
        server_default="0",
    )
    lifetime_charged = Column(
        Numeric(precision=14, scale=2),
        nullable=False,
        default=Decimal("0.00"),
        server_default="0",
    )
    lifetime_collected = Column(
        Numeric(precision=14, scale=2),
        nullable=False,
        default=Decimal("0.00"),
        server_default="0",
    )
    last_charged_at = Column(DateTime(timezone=True), nullable=True)
    last_collected_at = Column(DateTime(timezone=True), nullable=True)

    contract = relationship("CorporateContract", back_populates="prepayment_account")
    product_balances = relationship(
        "CorporatePrepaymentBalance",
        back_populates="account",
        cascade="all, delete-orphan",
    )
    ledger_entries = relationship(
        "CorporatePrepaymentLedger",
        back_populates="account",
        cascade="all, delete-orphan",
    )

    @property
    def tracked_products_count(self) -> int:
        return len(self.product_balances or [])

    @property
    def reserved_products_count(self) -> int:
        return sum(1 for balance in self.product_balances or [] if Decimal(str(balance.reserved_units or 0)) > 0)

    @property
    def debt_products_count(self) -> int:
        return sum(1 for balance in self.product_balances or [] if balance.available_units < 0)

    def to_dict(self):
        return {
            "id": self.id,
            "contract_id": self.contract_id,
            "is_active": self.is_active,
            "tracked_products_count": self.tracked_products_count,
            "reserved_products_count": self.reserved_products_count,
            "debt_products_count": self.debt_products_count,
            "outstanding_amount": float(self.outstanding_amount or 0),
            "lifetime_charged": float(self.lifetime_charged or 0),
            "lifetime_collected": float(self.lifetime_collected or 0),
            "last_charged_at": self.last_charged_at.isoformat() if self.last_charged_at else None,
            "last_collected_at": self.last_collected_at.isoformat() if self.last_collected_at else None,
            "last_topup_at": self.last_topup_at.isoformat() if self.last_topup_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CorporatePrepaymentBalance(db.Model, TimestampMixin):
    __tablename__ = "corporate_prepayment_balances"
    __table_args__ = (
        UniqueConstraint("account_id", "product_id", name="uq_corporate_prepayment_balance_account_product"),
        Index("idx_corporate_prepayment_balances_account_active", "account_id", "is_active"),
        Index("idx_corporate_prepayment_balances_product", "product_id"),
    )

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("corporate_prepayment_accounts.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    prepaid_units = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"))
    reserved_units = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"))
    consumed_units = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"))
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    last_topup_at = Column(DateTime(timezone=True), nullable=True)

    account = relationship("CorporatePrepaymentAccount", back_populates="product_balances")
    product = relationship(
        "Product",
        backref=backref("corporate_prepayment_balances", lazy="dynamic"),
    )
    ledger_entries = relationship(
        "CorporatePrepaymentLedger",
        back_populates="balance",
    )

    @property
    def available_units(self) -> Decimal:
        prepaid = Decimal(str(self.prepaid_units or 0))
        reserved = Decimal(str(self.reserved_units or 0))
        consumed = Decimal(str(self.consumed_units or 0))
        return prepaid - reserved - consumed

    @property
    def debt_units(self) -> Decimal:
        available = self.available_units
        return abs(available) if available < 0 else Decimal("0.00")

    def to_dict(self):
        return {
            "id": self.id,
            "account_id": self.account_id,
            "product_id": self.product_id,
            "product_name": getattr(self.product, "name", None),
            "product_sku": getattr(self.product, "sku", None),
            "product_size": getattr(getattr(self.product, "size", None), "value", getattr(self.product, "size", None)),
            "prepaid_units": float(self.prepaid_units or 0),
            "reserved_units": float(self.reserved_units or 0),
            "consumed_units": float(self.consumed_units or 0),
            "available_units": float(self.available_units),
            "debt_units": float(self.debt_units),
            "is_active": self.is_active,
            "last_topup_at": self.last_topup_at.isoformat() if self.last_topup_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CorporatePrepaymentLedger(db.Model, TimestampMixin):
    __tablename__ = "corporate_prepayment_ledger"
    __table_args__ = (
        Index("idx_corporate_prepayment_ledger_contract_created", "contract_id", "created_at"),
        Index("idx_corporate_prepayment_ledger_order_event", "order_id", "event_type"),
        Index("idx_corporate_prepayment_ledger_delivery_event", "delivery_id", "event_type"),
        Index("idx_corporate_prepayment_ledger_product_event", "product_id", "event_type"),
        UniqueConstraint("idempotency_key", name="uq_corporate_prepayment_ledger_idempotency"),
    )

    id = Column(Integer, primary_key=True)
    contract_id = Column(Integer, ForeignKey("corporate_contracts.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("corporate_prepayment_accounts.id"), nullable=False, index=True)
    # balance_id and product_id are required for UNITS-mode rows but NULL for
    # AMOUNT-mode rows (CHARGE/COLLECT). Enforced via DB CHECK constraint.
    balance_id = Column(Integer, ForeignKey("corporate_prepayment_balances.id"), nullable=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True, index=True)
    order_item_id = Column(Integer, ForeignKey("order_items.id"), nullable=True, index=True)
    delivery_id = Column(Integer, ForeignKey("deliveries.id"), nullable=True, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    event_type = Column(
        SqlEnum(
            CorporatePrepaymentEventType,
            name="corporate_prepayment_event_type",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        index=True,
    )
    # `units` is nullable now: required for UNITS-mode events
    # (TOPUP/RESERVE/CONSUME/RELEASE/ADJUSTMENT), NULL for AMOUNT-mode events
    # (CHARGE/COLLECT). Enforced via DB CHECK constraint.
    units = Column(Numeric(precision=12, scale=2), nullable=True)
    unit_price_snapshot = Column(Numeric(precision=12, scale=2), nullable=True)
    amount = Column(Numeric(precision=14, scale=2), nullable=True)
    currency = Column(String(3), nullable=False, default="UZS")
    transfer_reference = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)
    idempotency_key = Column(String(255), nullable=True)
    entry_metadata = Column(JSON, nullable=True, default=dict)

    contract = relationship("CorporateContract", back_populates="ledger_entries")
    account = relationship("CorporatePrepaymentAccount", back_populates="ledger_entries")
    balance = relationship("CorporatePrepaymentBalance", back_populates="ledger_entries")
    product = relationship("Product", backref=backref("corporate_prepayment_ledger_entries", lazy="dynamic"))
    order = relationship("Order", backref=backref("corporate_prepayment_ledger_entries", lazy="dynamic"))
    order_item = relationship("OrderItem", backref=backref("corporate_prepayment_ledger_entries", lazy="dynamic"))
    delivery = relationship("Delivery", backref=backref("corporate_prepayment_ledger_entries", lazy="dynamic"))
    actor_user = relationship("User", foreign_keys=[actor_user_id])

    def to_dict(self):
        return {
            "id": self.id,
            "contract_id": self.contract_id,
            "account_id": self.account_id,
            "balance_id": self.balance_id,
            "product_id": self.product_id,
            "product_name": getattr(self.product, "name", None),
            "order_id": self.order_id,
            "order_item_id": self.order_item_id,
            "delivery_id": self.delivery_id,
            "actor_user_id": self.actor_user_id,
            "event_type": self.event_type.value if hasattr(self.event_type, "value") else self.event_type,
            "units": float(self.units) if self.units is not None else None,
            "unit_price_snapshot": float(self.unit_price_snapshot) if self.unit_price_snapshot is not None else None,
            "amount": float(self.amount) if self.amount is not None else None,
            "currency": self.currency,
            "transfer_reference": self.transfer_reference,
            "notes": self.notes,
            "idempotency_key": self.idempotency_key,
            "entry_metadata": self.entry_metadata or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
