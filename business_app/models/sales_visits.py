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


class VisitPhoto(db.Model, TimestampMixin):
    """One photo taken at a visit, stored through FileStorageService (D17).

    `file_path` is a stored object, not a Telegram `file_id`: rotating the bot
    token invalidates every `file_id` ever issued, which is how the support
    inbox lost its media history. `telegram_file_unique_id` is kept only as a
    secondary reference back to the message the photo arrived on.

    `sha256` is the digest of the ORIGINAL bytes, taken before the storage
    service resizes anything, so two uploads of one photograph hash alike
    whatever the pipeline does to them. `duplicate_of_photo_id` points at the
    earliest photo carrying that digest FOR THE SAME AGENT — the service owns
    that lookup and publishes the answer as a field (`is_duplicate`); neither
    bot nor admin UI re-decides it. The index on `sha256` is therefore a lookup
    index and deliberately NOT unique: a repeat is recorded, never refused.
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
    file_path = Column(String(500), nullable=False)
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
    snapshot_at = Column(DateTime(timezone=True), nullable=False)

    agent = db.relationship("User", foreign_keys=[agent_user_id])

    def __repr__(self):
        return f"<SalesAgentDayPlan agent={self.agent_user_id} {self.plan_date} due={self.due_count}>"
