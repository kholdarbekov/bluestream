"""Scheduled price changes, the courtesy entries they grant and the notices they send.

Spec: docs/superpowers/specs/2026-11-16-price-change-transition-design.md (section 5.1 for the dates).

- `PriceChange` is one planned change of product prices: announced on `announce_on`, reminded
  `reminder_lead_days` before `change_on`, switched at 00:00 Tashkent on `change_on`. At most one change is
  pending (SCHEDULED or ANNOUNCED) at a time (`uq_price_changes_one_pending`).
- `PriceChangeItem` is one product's new price and its courtesy cap; `old_price` is stamped by the switch.
- `PriceCourtesy` is one person's entry: the right to buy up to the cap at the old price until the courtesy
  ends. `order_id` / `used_at` are set together by the claim and nulled together by a release.
- `PriceChangeNotice` is one announcement or reminder owed to one person; `channel` NULL means queued.

The vocabulary CHECKs are built with `_quoted` over the tuples below; the migration carries frozen copies,
pinned equal to these by tests/integration/test_price_change_pg_constraints.py. Enum columns are plain
VARCHAR(16) with a named CHECK (no Postgres TYPE).

`price_courtesies.user_id`, `price_change_notices.user_id` and `price_changes.created_by_id` are RESTRICT:
the account merge re-points them.
"""

import enum
from datetime import date, datetime, timedelta

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import relationship

from business_app import db
from business_app.models.base import TimestampMixin
from business_app.models.sales import _quoted
from business_app.utils.local_windows import local_day_bounds


class PriceChangeStatus(enum.Enum):
    SCHEDULED = "scheduled"
    ANNOUNCED = "announced"
    APPLIED = "applied"
    CANCELLED = "cancelled"


class CourtesySource(enum.Enum):
    AUTOMATIC = "automatic"
    MANAGER = "manager"


class PriceChangeNoticeKind(enum.Enum):
    ANNOUNCEMENT = "announcement"
    REMINDER = "reminder"


DEFAULT_REMINDER_LEAD_DAYS = 7
DEFAULT_COURTESY_DAYS = 14

PRICE_CHANGE_STATUSES = ("scheduled", "announced", "applied", "cancelled")
PENDING_PRICE_CHANGE_STATUSES = (PriceChangeStatus.SCHEDULED, PriceChangeStatus.ANNOUNCED)
COURTESY_SOURCES = ("automatic", "manager")
PRICE_CHANGE_NOTICE_KINDS = ("announcement", "reminder")
PRICE_CHANGE_NOTICE_CHANNELS = ("telegram", "email", "none")

_PENDING_PREDICATE = "status IN ('scheduled', 'announced')"

_STATUS_MATRIX = (
    "(status = 'applied') = (applied_at IS NOT NULL) "
    "AND (status = 'cancelled') = (cancelled_at IS NOT NULL) "
    "AND (status <> 'announced' OR announcement_started_at IS NOT NULL) "
    "AND (status <> 'scheduled' OR announcement_started_at IS NULL)"
)


def _enum(enum_class, name):
    return SqlEnum(
        enum_class,
        name=name,
        native_enum=False,
        length=16,
        values_callable=lambda members: [member.value for member in members],
    )


class PriceChange(db.Model, TimestampMixin):
    """One planned change of product prices."""

    __tablename__ = "price_changes"
    __table_args__ = (
        CheckConstraint(f"status IN ({_quoted(PRICE_CHANGE_STATUSES)})", name="ck_price_changes_status"),
        CheckConstraint(_STATUS_MATRIX, name="ck_price_changes_status_matrix"),
        CheckConstraint("change_on > announce_on", name="ck_price_changes_dates"),
        CheckConstraint("reminder_lead_days >= 1", name="ck_price_changes_reminder_lead_days"),
        CheckConstraint("courtesy_days >= 1", name="ck_price_changes_courtesy_days"),
        # At most one row can carry a true predicate: one pending change at a time. An expression index
        # alembic compares as text, so the migration test pins it through pg_indexes instead.
        Index(
            "uq_price_changes_one_pending",
            text(f"({_PENDING_PREDICATE})"),
            unique=True,
            postgresql_where=text(_PENDING_PREDICATE),
            sqlite_where=text(_PENDING_PREDICATE),
        ),
    )

    id = Column(Integer, primary_key=True)
    announce_on = Column(Date, nullable=False)
    change_on = Column(Date, nullable=False)
    reminder_lead_days = Column(Integer, nullable=False, default=DEFAULT_REMINDER_LEAD_DAYS)
    courtesy_days = Column(Integer, nullable=False, default=DEFAULT_COURTESY_DAYS)
    status = Column(
        _enum(PriceChangeStatus, "price_change_status"), nullable=False, default=PriceChangeStatus.SCHEDULED
    )
    announcement_started_at = Column(DateTime(timezone=True), nullable=True)
    reminder_started_at = Column(DateTime(timezone=True), nullable=True)
    applied_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_by_id = Column(
        Integer, ForeignKey("users.id", name="fk_price_changes_cancelled_by_id", ondelete="SET NULL"), nullable=True
    )
    created_by_id = Column(
        Integer, ForeignKey("users.id", name="fk_price_changes_created_by_id", ondelete="RESTRICT"), nullable=False
    )

    items = relationship(
        "PriceChangeItem", back_populates="price_change", cascade="all, delete-orphan", order_by="PriceChangeItem.id"
    )
    created_by = relationship("User", foreign_keys=[created_by_id])
    cancelled_by = relationship("User", foreign_keys=[cancelled_by_id])

    @property
    def effective_at(self) -> datetime:
        """The UTC instant the new prices start: 00:00 Tashkent on `change_on`."""
        return local_day_bounds(self.change_on)[0]

    @property
    def reminder_on(self) -> date:
        return self.change_on - timedelta(days=self.reminder_lead_days)

    @property
    def courtesy_last_day(self) -> date:
        """The last local day an order can still use the courtesy."""
        return self.change_on + timedelta(days=self.courtesy_days - 1)

    @property
    def courtesy_ends_at(self) -> datetime:
        """The UTC instant the courtesy ends: 00:00 Tashkent on `change_on + courtesy_days`."""
        return local_day_bounds(self.change_on + timedelta(days=self.courtesy_days))[0]

    @property
    def price_valid_until(self) -> date:
        """The last local day the old prices are the list prices."""
        return self.change_on - timedelta(days=1)


class PriceChangeItem(db.Model):
    """One product's new price inside a price change."""

    __tablename__ = "price_change_items"
    __table_args__ = (
        UniqueConstraint("price_change_id", "product_id", name="uq_price_change_items_change_product"),
        CheckConstraint("new_price > 0", name="ck_price_change_items_new_price"),
        CheckConstraint("courtesy_cap >= 1", name="ck_price_change_items_courtesy_cap"),
        Index("ix_price_change_items_product_id", "product_id"),
    )

    id = Column(Integer, primary_key=True)
    price_change_id = Column(
        Integer,
        ForeignKey("price_changes.id", name="fk_price_change_items_price_change_id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id = Column(Integer, ForeignKey("products.id", name="fk_price_change_items_product_id"), nullable=False)
    new_price = Column(Numeric(10, 2), nullable=False)
    courtesy_cap = Column(Integer, nullable=False)
    # Stamped by the switch: the price the product had before it.
    old_price = Column(Numeric(10, 2), nullable=True)

    price_change = relationship("PriceChange", back_populates="items")
    product = relationship("Product")


class PriceCourtesy(db.Model):
    """One person's right to buy at the old price after the change."""

    __tablename__ = "price_courtesies"
    __table_args__ = (
        UniqueConstraint("price_change_id", "user_id", name="uq_price_courtesies_change_user"),
        CheckConstraint(f"source IN ({_quoted(COURTESY_SOURCES)})", name="ck_price_courtesies_source"),
        CheckConstraint("(order_id IS NULL) = (used_at IS NULL)", name="ck_price_courtesies_used"),
        CheckConstraint("source <> 'manager' OR reason IS NOT NULL", name="ck_price_courtesies_manager_reason"),
        Index("ix_price_courtesies_user_id", "user_id"),
        Index("ix_price_courtesies_order_id", "order_id"),
    )

    id = Column(Integer, primary_key=True)
    price_change_id = Column(
        Integer, ForeignKey("price_changes.id", name="fk_price_courtesies_price_change_id"), nullable=False
    )
    # The person's anchor account.
    user_id = Column(
        Integer, ForeignKey("users.id", name="fk_price_courtesies_user_id", ondelete="RESTRICT"), nullable=False
    )
    source = Column(_enum(CourtesySource, "courtesy_source"), nullable=False)
    reason = Column(Text, nullable=True)
    granted_by_id = Column(
        Integer, ForeignKey("users.id", name="fk_price_courtesies_granted_by_id", ondelete="SET NULL"), nullable=True
    )
    granted_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    # Set together with used_at by the claim; a release nulls both.
    order_id = Column(Integer, ForeignKey("orders.id", name="fk_price_courtesies_order_id"), nullable=True)
    used_at = Column(DateTime(timezone=True), nullable=True)

    price_change = relationship("PriceChange")
    user = relationship("User", foreign_keys=[user_id])
    granted_by = relationship("User", foreign_keys=[granted_by_id])
    order = relationship("Order", foreign_keys=[order_id])


class PriceChangeNotice(db.Model):
    """One announcement or reminder owed to one person."""

    __tablename__ = "price_change_notices"
    __table_args__ = (
        UniqueConstraint("price_change_id", "user_id", "kind", name="uq_price_change_notices_change_user_kind"),
        CheckConstraint(f"kind IN ({_quoted(PRICE_CHANGE_NOTICE_KINDS)})", name="ck_price_change_notices_kind"),
        CheckConstraint(
            f"channel IS NULL OR channel IN ({_quoted(PRICE_CHANGE_NOTICE_CHANNELS)})",
            name="ck_price_change_notices_channel",
        ),
        CheckConstraint("channel <> 'none' OR sent_at IS NULL", name="ck_price_change_notices_none_unsent"),
        CheckConstraint("channel IS NOT NULL OR sent_at IS NULL", name="ck_price_change_notices_queued_unsent"),
        Index("ix_price_change_notices_user_id", "user_id"),
    )

    id = Column(Integer, primary_key=True)
    price_change_id = Column(
        Integer, ForeignKey("price_changes.id", name="fk_price_change_notices_price_change_id"), nullable=False
    )
    user_id = Column(
        Integer, ForeignKey("users.id", name="fk_price_change_notices_user_id", ondelete="RESTRICT"), nullable=False
    )
    kind = Column(_enum(PriceChangeNoticeKind, "price_change_notice_kind"), nullable=False)
    # NULL = queued, not yet sent.
    channel = Column(String(16), nullable=True)
    queued_at = Column(DateTime(timezone=True), nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(String(255), nullable=True)

    user = relationship("User", foreign_keys=[user_id])
