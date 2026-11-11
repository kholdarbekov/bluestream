"""Shared-place cases: a first-order customer handed back empties the place's balance does not explain.

Spec: docs/superpowers/specs/2026-11-10-shared-place-detection-design.md (§9 binds over earlier sections).

A case is opened when such a delivery is completed. The driver is asked one question in staff_bot (which
nearby customer, if any, do these bottles belong to); an admin then resolves the case. One case per order.

- There is NO customer column: the customer is `order.user_id`.
- `candidates` is the frozen list of nearby places found at open (max 3, nearest first). The admin screen
  shows them live; the frozen copy is what the driver was asked about.
- `resolution_kind` + `resolution_payload` are the CLAIM while the case is being resolved and the outcome
  afterwards; a replay compares against the payload. A `closed` case keeps `resolution_kind` NULL but
  stores its payload too.
- `asked_driver_id` is NULL for an admin-entered case (no driver was asked). `asked_driver_id` and
  `decided_by_user_id` are ON DELETE SET NULL, and the account merge re-points both.
- `alert_posted_at` is the Slack claim marker (the alert carries no customer data).

The vocabulary CHECKs are built with `_quoted` over the tuples below; the migration carries frozen copies,
pinned equal to these by tests/integration/test_shared_place_case_pg_constraints.py.
"""

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)

from business_app import db
from business_app.models.base import TimestampMixin
from business_app.models.sales import _quoted

SHARED_PLACE_CASE_STATUSES = ("awaiting_driver", "awaiting_admin", "resolved", "closed")
SHARED_PLACE_OPEN_STATUSES = ("awaiting_driver", "awaiting_admin")
SHARED_PLACE_DRIVER_ANSWERS = ("picked", "none", "skipped")
SHARED_PLACE_RESOLUTION_KINDS = ("shared_place", "same_home", "other_place", "own_bottles", "wrong_count")
SHARED_PLACE_EMPTY_REASONS = ("no_pin", "none_within_radius", "none_eligible")
# Every choice the case screen can offer: the five resolution kinds, then Close (which records no kind).
SHARED_PLACE_CHOICE_KINDS = SHARED_PLACE_RESOLUTION_KINDS + ("close",)
# The driver's answer as the admin screen and Slack show it: "none" on a case that offered no candidates was typed on
# the note-only screen, so it is never "None of these" (spec §4.4). Not stored: derived by `answer_kind`.
SHARED_PLACE_NO_CANDIDATES_ANSWER = "no_candidates"

TABLE = "shared_place_cases"


class SharedPlaceCase(db.Model, TimestampMixin):
    """One shared-place question about one order's returned empties."""

    __tablename__ = TABLE
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_shared_place_cases_order_id"),
        CheckConstraint(f"status IN ({_quoted(SHARED_PLACE_CASE_STATUSES)})", name="ck_shared_place_cases_status"),
        CheckConstraint(
            f"driver_answer IS NULL OR driver_answer IN ({_quoted(SHARED_PLACE_DRIVER_ANSWERS)})",
            name="ck_shared_place_cases_driver_answer",
        ),
        CheckConstraint(
            f"resolution_kind IS NULL OR resolution_kind IN ({_quoted(SHARED_PLACE_RESOLUTION_KINDS)})",
            name="ck_shared_place_cases_resolution_kind",
        ),
        CheckConstraint(
            f"empty_reason IS NULL OR empty_reason IN ({_quoted(SHARED_PLACE_EMPTY_REASONS)})",
            name="ck_shared_place_cases_empty_reason",
        ),
        CheckConstraint("returned_qty >= 1", name="ck_shared_place_cases_returned_qty"),
        CheckConstraint("(driver_answer IS NULL) = (answered_at IS NULL)", name="ck_shared_place_cases_answered"),
        CheckConstraint(
            "(COALESCE(driver_answer, '') = 'picked') = (picked_index IS NOT NULL)", name="ck_shared_place_cases_picked"
        ),
        CheckConstraint(
            "(status IN ('resolved', 'closed')) = (decided_at IS NOT NULL)", name="ck_shared_place_cases_decided"
        ),
        CheckConstraint(
            "status <> 'resolved' OR resolution_kind IS NOT NULL", name="ck_shared_place_cases_resolved_kind"
        ),
        CheckConstraint("status <> 'closed' OR resolution_kind IS NULL", name="ck_shared_place_cases_closed_kind"),
        Index("ix_shared_place_cases_status_created_at", "status", "created_at"),
        Index("ix_shared_place_cases_delivery_id", "delivery_id"),
        Index("ix_shared_place_cases_asked_driver_id", "asked_driver_id"),
    )

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id", name="fk_shared_place_cases_order_id"), nullable=False)
    # NULL on the admin Orders path, which delivers an order with no Delivery row.
    delivery_id = Column(Integer, ForeignKey("deliveries.id", name="fk_shared_place_cases_delivery_id"), nullable=True)
    # The delivery address, frozen at open.
    address_id = Column(Integer, ForeignKey("addresses.id", name="fk_shared_place_cases_address_id"), nullable=False)
    returned_qty = Column(Integer, nullable=False)
    place_balance_before = Column(Numeric(12, 2), nullable=False)
    asked_driver_id = Column(
        Integer,
        ForeignKey("users.id", name="fk_shared_place_cases_asked_driver_id", ondelete="SET NULL"),
        nullable=True,
    )
    candidates = Column(JSON, nullable=False)
    empty_reason = Column(String(20), nullable=True)
    status = Column(String(16), nullable=False, default="awaiting_driver")
    driver_answer = Column(String(8), nullable=True)
    picked_index = Column(Integer, nullable=True)
    # Never edited once written.
    driver_note = Column(Text, nullable=True)
    answered_at = Column(DateTime(timezone=True), nullable=True)
    alert_posted_at = Column(DateTime(timezone=True), nullable=True)
    resolution_kind = Column(String(16), nullable=True)
    resolution_payload = Column(JSON, nullable=True)
    decision_reason = Column(Text, nullable=True)
    decided_by_user_id = Column(
        Integer,
        ForeignKey("users.id", name="fk_shared_place_cases_decided_by_user_id", ondelete="SET NULL"),
        nullable=True,
    )
    decided_at = Column(DateTime(timezone=True), nullable=True)
    resulting_group_id = Column(
        Integer,
        ForeignKey("address_groups.id", name="fk_shared_place_cases_resulting_group_id", ondelete="SET NULL"),
        nullable=True,
    )

    order = db.relationship("Order", foreign_keys=[order_id])
    delivery = db.relationship("Delivery", foreign_keys=[delivery_id])
    address = db.relationship("UserAddress", foreign_keys=[address_id])
    asked_driver = db.relationship("User", foreign_keys=[asked_driver_id])
    decided_by = db.relationship("User", foreign_keys=[decided_by_user_id])

    @property
    def answer_kind(self):
        """The driver's answer as every admin surface shows it (the list, the case, Slack): the stored answer, except
        `SHARED_PLACE_NO_CANDIDATES_ANSWER` for a "none" given when no candidates were offered."""
        if self.driver_answer == "none" and not self.candidates:
            return SHARED_PLACE_NO_CANDIDATES_ANSWER
        return self.driver_answer

    def __repr__(self):
        return f"<SharedPlaceCase order={self.order_id} {self.status}>"
