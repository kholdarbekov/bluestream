"""Sales-agent visit models (phase 2a): the visit state machine, the per-product
stock check taken at the shelf, and the store owner's order-confirmation request.

A Visit is the unit of field work: one agent, one outlet, one open at a time
(partial unique ``uq_visits_one_open_per_agent``; VisitService checks before the
insert AND translates this index's IntegrityError after it, so the API answers 409
SALES_VISIT_ALREADY_OPEN on both sides of the double-tap race).
``current_step`` is the server-defined resume point — the staff bot renders
whatever screen it names and never decides the step itself.

The CHECK expressions here mirror migration d4e7f9a2b6c1 one-for-one; the
``_quoted`` helper is imported from models/sales.py so the quoting rule has a
single implementation.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text

from business_app import db
from business_app.models.base import TimestampMixin
from business_app.models.sales import _quoted

VISIT_STATUSES = ("in_progress", "completed", "abandoned")
VISIT_STEPS = ("checkin", "stock", "order", "close")
VISIT_OUTCOMES = ("order_placed", "no_order", "closed", "owner_absent", "refused")
NO_ORDER_REASONS = ("sufficient_stock", "cash_issue", "price", "competitor", "other")
RATE_SOURCES = ("stock_checks", "orders", "none")
CONFIRMATION_STATUSES = ("pending", "confirmed", "declined", "expired")
# The same-day approval hold's row vocabulary. The staff-bot-facing agent order states live in
# shared/staff_constants.py (SALES_AGENT_ORDER_STATES).
AGENT_ORDER_APPROVAL_STATUSES = ("pending", "approved", "rejected", "cancelled")
PHOTO_KINDS = ("storefront", "shelf", "other")


class Visit(db.Model, TimestampMixin):
    """One agent's visit to one outlet, from check-in to close."""

    __tablename__ = "visits"
    __table_args__ = (
        CheckConstraint(f"status IN ({_quoted(VISIT_STATUSES)})", name="ck_visits_status"),
        CheckConstraint(f"current_step IN ({_quoted(VISIT_STEPS)})", name="ck_visits_current_step"),
        CheckConstraint(
            f"outcome IS NULL OR outcome IN ({_quoted(VISIT_OUTCOMES)})",
            name="ck_visits_outcome",
        ),
        CheckConstraint(
            f"no_order_reason IS NULL OR no_order_reason IN ({_quoted(NO_ORDER_REASONS)})",
            name="ck_visits_no_order_reason",
        ),
        CheckConstraint(
            "(checkin_latitude IS NULL) = (checkin_longitude IS NULL)",
            name="ck_visits_checkin_coords_pair",
        ),
        Index("ix_visits_outlet_id", "outlet_id"),
        Index("ix_visits_agent_user_id", "agent_user_id"),
        Index("ix_visits_started_at", "started_at"),
        # At most one OPEN visit per agent. sqlite_where mirrors postgresql_where
        # so the SQLite test schema keeps the partial filter instead of degrading
        # to a full unique on agent_user_id.
        Index(
            "uq_visits_one_open_per_agent",
            "agent_user_id",
            unique=True,
            postgresql_where=sa_text("status = 'in_progress'"),
            sqlite_where=sa_text("status = 'in_progress'"),
        ),
    )

    id = Column(Integer, primary_key=True)
    outlet_id = Column(Integer, ForeignKey("outlets.id", name="fk_visits_outlet_id"), nullable=False)
    agent_user_id = Column(Integer, ForeignKey("users.id", name="fk_visits_agent_user_id"), nullable=False)
    status = Column(String(20), nullable=False, default="in_progress")
    planned = Column(Boolean, nullable=False, default=False)
    current_step = Column(String(20), nullable=False, default="checkin")
    started_at = Column(DateTime(timezone=True), nullable=False)
    checkin_at = Column(DateTime(timezone=True), nullable=True)
    checkin_latitude = Column(Float, nullable=True)
    checkin_longitude = Column(Float, nullable=True)
    checkin_accuracy_m = Column(Float, nullable=True)
    distance_m = Column(Float, nullable=True)
    in_radius = Column(Boolean, nullable=True)
    checkin_skipped = Column(Boolean, nullable=False, default=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    outcome = Column(String(30), nullable=True)
    no_order_reason = Column(String(30), nullable=True)
    dm_present = Column(Boolean, nullable=True)
    notes = Column(Text, nullable=True)
    next_visit_at = Column(Date, nullable=True)

    outlet = db.relationship("Outlet", foreign_keys=[outlet_id])
    agent = db.relationship("User", foreign_keys=[agent_user_id])
    stock_checks = db.relationship(
        "VisitStockCheck",
        backref="visit",
        cascade="all, delete-orphan",
        order_by="VisitStockCheck.product_id",
    )
    # Deleting a visit would have to delete its photos, and a photo in ANOTHER
    # visit may point at one of them through duplicate_of_photo_id — so this
    # cascade raises rather than silently losing the provenance of a flagged
    # duplicate. Nothing in production deletes a visit; a visit that went
    # nowhere is `abandoned`, not removed.
    photos = db.relationship(
        "VisitPhoto",
        backref="visit",
        cascade="all, delete-orphan",
        order_by="VisitPhoto.id",
    )
    # At most one order per visit (partial unique uq_orders_visit_id).
    order = db.relationship("Order", foreign_keys="Order.visit_id", uselist=False, backref="visit")

    def __repr__(self):
        return f"<Visit {self.id} outlet={self.outlet_id} agent={self.agent_user_id} {self.status}>"


class VisitStockCheck(db.Model, TimestampMixin):
    """What the agent counted on the shelf for one product at one visit."""

    __tablename__ = "visit_stock_checks"
    __table_args__ = (
        UniqueConstraint("visit_id", "product_id", name="uq_visit_stock_checks_visit_product"),
        CheckConstraint(
            f"rate_source IS NULL OR rate_source IN ({_quoted(RATE_SOURCES)})",
            name="ck_visit_stock_checks_rate_source",
        ),
        Index("ix_visit_stock_checks_visit_id", "visit_id"),
        Index("ix_visit_stock_checks_product_id", "product_id"),
    )

    id = Column(Integer, primary_key=True)
    visit_id = Column(Integer, ForeignKey("visits.id", name="fk_visit_stock_checks_visit_id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id", name="fk_visit_stock_checks_product_id"), nullable=False)
    on_hand_qty = Column(Integer, nullable=False, default=0)
    empties_qty = Column(Integer, nullable=True)
    is_sold_out = Column(Boolean, nullable=False, default=False)
    is_low = Column(Boolean, nullable=False, default=False)
    suggested_qty = Column(Integer, nullable=True)
    accepted_qty = Column(Integer, nullable=True)
    rate_per_day = Column(Numeric(precision=8, scale=3), nullable=True)
    rate_source = Column(String(20), nullable=True)

    product = db.relationship("Product", foreign_keys=[product_id])

    def __repr__(self):
        return f"<VisitStockCheck visit={self.visit_id} product={self.product_id} on_hand={self.on_hand_qty}>"


class OrderConfirmationRequest(db.Model, TimestampMixin):
    """The Confirm/Decline the store owner is asked for after an agent order."""

    __tablename__ = "order_confirmation_requests"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_order_confirmation_requests_request_id"),
        CheckConstraint(
            f"status IN ({_quoted(CONFIRMATION_STATUSES)})",
            name="ck_order_confirmation_requests_status",
        ),
        Index("ix_order_confirmation_requests_order_id", "order_id"),
        Index("ix_order_confirmation_requests_status", "status"),
        Index("ix_order_confirmation_requests_expires_at", "expires_at"),
    )

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id", name="fk_order_confirmation_requests_order_id"), nullable=False)
    outlet_id = Column(
        Integer, ForeignKey("outlets.id", name="fk_order_confirmation_requests_outlet_id"), nullable=False
    )
    status = Column(String(20), nullable=False, default="pending")
    requested_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    responded_at = Column(DateTime(timezone=True), nullable=True)
    response_channel = Column(String(20), nullable=True)
    decline_reason = Column(Text, nullable=True)
    # Webhook dedup key handed to the customer bot; secrets.token_hex(8) = 16 chars.
    request_id = Column(String(16), nullable=False)

    order = db.relationship("Order", foreign_keys=[order_id])
    outlet = db.relationship("Outlet", foreign_keys=[outlet_id])

    def __repr__(self):
        return f"<OrderConfirmationRequest order={self.order_id} {self.status}>"


class AgentOrderApproval(db.Model, TimestampMixin):
    """The same-day staff approval hold on one agent order (C14).

    An agent's second or later order at one outlet on one local day is held here. The decision
    is made at placement, under an outlet row lock. Only the manager/admin queue confirms a held
    order: every automatic confirmer skips it, and every explicit one is refused.

    This is a table of its own, not a kind of `order_confirmation_requests` row, because every
    reader of that table picks the latest pending row by `order_id` alone. The store's answer,
    the push task and the expiry sweep would each reach a staff hold.

    The CHECKs enforce the state matrix:

    | status    | decided_at | decided_by_user_id | reason |
    | pending   | NULL       | NULL               | NULL   |
    | approved  | set        | set                | NULL   |
    | rejected  | set        | set                | set    |
    | cancelled | set        | set, or NULL for a system path | NULL |

    `reason` is the rejecter's. The same text goes to the cancel history row's staff-only
    `reason`, never to its customer-visible `notes`.
    """

    __tablename__ = "agent_order_approvals"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_agent_order_approvals_order_id"),
        CheckConstraint(
            f"status IN ({_quoted(AGENT_ORDER_APPROVAL_STATUSES)})", name="ck_agent_order_approvals_status"
        ),
        CheckConstraint("(status = 'pending') = (decided_at IS NULL)", name="ck_agent_order_approvals_decided"),
        CheckConstraint(
            "(status NOT IN ('approved', 'rejected') OR decided_by_user_id IS NOT NULL) "
            "AND (status <> 'pending' OR decided_by_user_id IS NULL)",
            name="ck_agent_order_approvals_decider",
        ),
        CheckConstraint("(status = 'rejected') = (reason IS NOT NULL)", name="ck_agent_order_approvals_reason"),
        Index("ix_agent_order_approvals_status_requested_at", "status", "requested_at"),
        Index("ix_agent_order_approvals_agent_user_id", "agent_user_id"),
    )

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id", name="fk_agent_order_approvals_order_id"), nullable=False)
    outlet_id = Column(Integer, ForeignKey("outlets.id", name="fk_agent_order_approvals_outlet_id"), nullable=False)
    # `order.created_by_staff_id`: the queue's agent filter, and one of the two self-decision subjects.
    agent_user_id = Column(
        Integer, ForeignKey("users.id", name="fk_agent_order_approvals_agent_user_id"), nullable=False
    )
    # Frozen evidence: the earlier same-day orders found at placement. The queue shows them live.
    earlier_order_ids = Column(JSON, nullable=False)
    status = Column(String(12), nullable=False, default="pending")
    requested_at = Column(DateTime(timezone=True), nullable=False)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decided_by_user_id = Column(
        Integer, ForeignKey("users.id", name="fk_agent_order_approvals_decided_by_user_id"), nullable=True
    )
    reason = Column(Text, nullable=True)

    order = db.relationship("Order", foreign_keys=[order_id])
    outlet = db.relationship("Outlet", foreign_keys=[outlet_id])
    agent = db.relationship("User", foreign_keys=[agent_user_id])
    decided_by = db.relationship("User", foreign_keys=[decided_by_user_id])

    def __repr__(self):
        return f"<AgentOrderApproval order={self.order_id} {self.status}>"


class VisitPhoto(db.Model, TimestampMixin):
    """One photo taken at a visit, kept on Telegram (D27, reversing D17).

    We never store the picture: `telegram_file_id` is the STAFF bot's id for it, and the admin UI
    streams it through `VisitService.stream_photo` on demand. A file id works only through the bot
    that received it, so replacing the staff bot makes these rows unviewable -- an accepted
    consequence (spec D27). `telegram_file_unique_id` is kept as the stable cross-bot reference.

    `sha256` is the digest of the bytes the bot downloaded into memory, so two uploads of one
    photograph hash alike although Telegram gives each upload a new file id.
    `duplicate_of_photo_id` points at the earliest photo carrying that digest FOR THE SAME AGENT --
    the service owns that lookup and publishes the answer as a field (`is_duplicate`); neither bot
    nor admin UI re-decides it. The index on `sha256` is therefore a lookup index and deliberately
    NOT unique: a repeat is recorded, never refused.
    """

    __tablename__ = "visit_photos"
    __table_args__ = (
        CheckConstraint(f"kind IN ({_quoted(PHOTO_KINDS)})", name="ck_visit_photos_kind"),
        Index("ix_visit_photos_visit_id", "visit_id"),
        Index("ix_visit_photos_sha256", "sha256"),
    )

    id = Column(Integer, primary_key=True)
    visit_id = Column(Integer, ForeignKey("visits.id", name="fk_visit_photos_visit_id"), nullable=False)
    kind = Column(String(20), nullable=False)
    telegram_file_id = Column(String(255), nullable=False)
    sha256 = Column(String(64), nullable=False)
    telegram_file_unique_id = Column(String(100), nullable=True)
    received_at = Column(DateTime(timezone=True), nullable=False)
    duplicate_of_photo_id = Column(
        Integer,
        ForeignKey("visit_photos.id", name="fk_visit_photos_duplicate_of_photo_id"),
        nullable=True,
    )

    def __repr__(self):
        return f"<VisitPhoto {self.id} visit={self.visit_id} {self.kind}>"


class SalesAgentDayPlan(db.Model, TimestampMixin):
    """One agent's plan for one LOCAL day: how many shops were due, how many late.

    A snapshot, because the number is otherwise unrecoverable. `outlets.
    next_visit_due_at` is a single mutable column with no history: the 01:00 job
    rewrites it for every non-lost outlet, every visit close republishes it, and
    so does every stage move. "How many shops were due for this agent on the 16th"
    can therefore be asked only on the 16th — and plan-vs-fact is a question about
    last week. Deriving it backwards would read today's class, today's assignment
    and today's catalogue against yesterday's calendar and would change its answer
    for a past day every time anyone edits an outlet, silently.

    One row per active agent per day, INCLUDING a day with nothing due (zeroes):
    a missing row means "we were not snapshotting yet" and is published as
    `plan_source: "none"`, which is a different thing from a quiet Tuesday.
    `snapshot_at` is the job's instant, kept so a re-run is visible.
    """

    __tablename__ = "sales_agent_day_plans"
    __table_args__ = (
        # One plan per agent per day — the upsert's backstop, and what makes a
        # retried 01:20 job idempotent rather than a doubled denominator.
        UniqueConstraint("agent_user_id", "plan_date", name="uq_sales_agent_day_plans_agent_date"),
        Index("ix_sales_agent_day_plans_plan_date", "plan_date"),
    )

    id = Column(Integer, primary_key=True)
    agent_user_id = Column(
        Integer, ForeignKey("users.id", name="fk_sales_agent_day_plans_agent_user_id"), nullable=False
    )
    plan_date = Column(Date, nullable=False)
    due_count = Column(Integer, nullable=False, default=0)
    overdue_count = Column(Integer, nullable=False, default=0)
    # The outlet ids behind `due_count`, frozen by the same 01:20 query that counts them, so pay
    # and plan-vs-fact judge a past day against what was actually due. NULL on rows written
    # before this column existed; readers then fall back to the legacy count.
    due_outlet_ids = Column(JSON, nullable=True)
    snapshot_at = Column(DateTime(timezone=True), nullable=False)

    agent = db.relationship("User", foreign_keys=[agent_user_id])

    def __repr__(self):
        return f"<SalesAgentDayPlan agent={self.agent_user_id} {self.plan_date} due={self.due_count}>"
