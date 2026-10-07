"""Sales-agent pay models: plans and their versions, agent terms, company months, unpaid days,
the earnings ledger, new-outlet checks, penalties, adjustments and frozen statements.

Spec: docs/superpowers/specs/2026-09-28-sales-agent-compensation-design.md §3 and §3.5b. Every
constraint here is named and mirrors migrations b7e3c1a95d42 and c5d8e2f1a7b3 (v5 tiers) one for
one. The vocabulary CHECKs are built
with `_quoted` over the `shared/staff_constants.py` tuples, so the model and the service offer
the same values.

Rules every table follows (§3.1):
- A month is a DATE on the 1st. Only `local_windows.month_start()` / `local_month()` write it,
  and on Postgres a CHECK backs that up (`_first_of_month`, which `create_all` skips on SQLite).
- Money is Numeric(14, 2) holding whole UZS (`_whole`). `pay_rules.round_uzs` is the only place
  that rounds; the CHECK only catches a missed step.
- Insert-only tables carry `created_at` alone; tables with mutable rows use TimestampMixin.
- Relationships are lookup-only: no cascade, and no backref on Order, Outlet or Product.
- There are no triggers. Who may write which row, and when, is guarded by the pay services
  under a period row lock (§3.4).
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)

from business_app import db
from business_app.models.base import TimestampMixin
from business_app.models.sales import _quoted
from business_app.models.translatable import TranslatableMixin, translatable
from business_app.utils.timezone_utils import get_utc_now
from shared.staff_constants import (
    SALES_PAY_ADJUSTMENT_SOURCES,
    SALES_PAY_DIFFERENCE_CAUSES,
    SALES_PAY_LEDGER_KINDS,
    SALES_PAY_OUTLET_CHECK_STATUSES,
    SALES_PAY_PENALTY_ORIGINS,
    SALES_PAY_PENALTY_STATUSES,
    SALES_PAY_PERIOD_STATUSES,
    SALES_PAY_RATE_MODES,
    SALES_PAY_REVERSAL_CAUSES,
)


def _fk(table, column, target, *, nullable=False):
    """An integer FK named `fk_<table>_<column>` (CONVENTIONS.md §5)."""
    return Column(Integer, ForeignKey(f"{target}.id", name=f"fk_{table}_{column}"), nullable=nullable)


def _money(*, nullable=False):
    return Column(Numeric(14, 2), nullable=nullable)


def _created_at():
    """The one timestamp of an insert-only row."""
    return Column(DateTime(timezone=True), nullable=False, default=get_utc_now)


def _whole(table, *columns):
    """Every stored pay amount is whole UZS; the CHECK only catches a missed `round_uzs`."""
    name = f"ck_{table}_{columns[0]}_whole" if len(columns) == 1 else f"ck_{table}_amounts_whole"
    return CheckConstraint(" AND ".join(f"{column} = ROUND({column}, 0)" for column in columns), name=name)


def _first_of_month(table, column):
    """A month column holds the 1st. Postgres only: `create_all` on SQLite skips it."""
    return CheckConstraint(f"EXTRACT(DAY FROM {column}) = 1", name=f"ck_{table}_{column}_first_of_month").ddl_if(
        dialect="postgresql"
    )


def _rate_value_rule(mode, value):
    """A rate is never negative; a percent is at most 100; a per-unit rate is whole UZS."""
    return (
        f"{value} >= 0 AND ({mode} <> 'percent' OR {value} <= 100) "
        f"AND ({mode} <> 'per_unit' OR {value} = ROUND({value}, 0))"
    )


class SalesPayPlan(db.Model, TimestampMixin):
    """A named family of plan versions. Never deleted or deactivated; an agent's terms point at it."""

    __tablename__ = "sales_pay_plans"
    __table_args__ = (UniqueConstraint("name", name="uq_sales_pay_plans_name"),)

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    created_by_user_id = _fk(__tablename__, "created_by_user_id", "users")

    versions = db.relationship("SalesPayPlanVersion", back_populates="plan")

    def __repr__(self):
        return f"<SalesPayPlan {self.id} {self.name!r}>"


class SalesPayPlanVersion(db.Model):
    """One version of a plan, effective from a month. Insert-only.

    Its commission is its tier rows (`rates`): the rows with `product_id` NULL are the default
    schedule, and every other product's rows are that product's own schedule (§3.2, V5-B2). The
    version row holds no rate, so the default has one home.

    A correction is a newer version for the same month: `SalesPayPlanService.resolve` takes the
    latest `effective_month <= month`, and within a month the higher `version_no`.
    `gate_bands` is `[{"min_pct": "80.0", "multiplier": "1.000"}, ...]` in descending `min_pct`,
    ending at "0.0"; the service validates its shape.
    """

    __tablename__ = "sales_pay_plan_versions"
    __table_args__ = (
        UniqueConstraint("plan_id", "version_no", name="uq_sales_pay_plan_versions_plan_version"),
        CheckConstraint(
            "gate_min_visits_due >= 0 AND bonus_amount >= 0 AND bonus_window_days >= 1 "
            "AND bonus_min_orders_with_total >= 1 AND bonus_min_combined_total >= 0 "
            "AND bonus_min_orders_any_amount >= 1 AND bonus_prior_customer_lookback_days >= 0 "
            "AND version_no >= 1",
            name="ck_sales_pay_plan_versions_ranges",
        ),
        _whole(__tablename__, "bonus_amount", "bonus_min_combined_total"),
        _first_of_month(__tablename__, "effective_month"),
        Index("ix_sales_pay_plan_versions_plan_month", "plan_id", "effective_month"),
    )

    id = Column(Integer, primary_key=True)
    plan_id = _fk(__tablename__, "plan_id", "sales_pay_plans")
    version_no = Column(Integer, nullable=False)
    effective_month = Column(Date, nullable=False)
    gate_bands = Column(JSON, nullable=False)
    gate_min_visits_due = Column(Integer, nullable=False)
    bonus_amount = _money()
    bonus_window_days = Column(Integer, nullable=False)
    bonus_min_orders_with_total = Column(Integer, nullable=False)
    bonus_min_combined_total = _money()
    bonus_min_orders_any_amount = Column(Integer, nullable=False)
    bonus_prior_customer_lookback_days = Column(Integer, nullable=False)
    note = Column(Text, nullable=True)
    created_by_user_id = _fk(__tablename__, "created_by_user_id", "users")
    created_at = _created_at()

    plan = db.relationship("SalesPayPlan", back_populates="versions")
    rates = db.relationship("SalesPayPlanRate", back_populates="version")  # every tier, the default schedule's too
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])

    def __repr__(self):
        return f"<SalesPayPlanVersion plan={self.plan_id} v{self.version_no} from={self.effective_month}>"


class SalesPayPlanRate(db.Model):
    """One TIER of one schedule of a version (§3.2, I-29), written in the version's transaction.
    Insert-only.

    `product_id` NULL is the version's default schedule. `from_unit` is the tier's first unit; the
    upper bound is not stored (`TierSchedule.bounds()`). "Every version has a default schedule" and
    "a schedule starts at unit 1" span rows, so `SalesPayPlanService.validate` guards them and
    `_resolved` refuses to rate a version that breaks them. NULLs are distinct in the composite
    unique, so the default schedule's own unique is a partial index, which both dialects enforce.
    The FK to `products` refuses a product hard-delete that would orphan a tier; the product itself
    may be inactive.
    """

    __tablename__ = "sales_pay_plan_rates"
    __table_args__ = (
        UniqueConstraint(
            "plan_version_id", "product_id", "from_unit", name="uq_sales_pay_plan_rates_version_product_from"
        ),
        Index(
            "uq_sales_pay_plan_rates_version_default_from",
            "plan_version_id",
            "from_unit",
            unique=True,
            postgresql_where=text("product_id IS NULL"),
            sqlite_where=text("product_id IS NULL"),
        ),
        CheckConstraint("from_unit >= 1", name="ck_sales_pay_plan_rates_from_unit"),
        CheckConstraint(f"rate_mode IN ({_quoted(SALES_PAY_RATE_MODES)})", name="ck_sales_pay_plan_rates_rate_mode"),
        CheckConstraint(_rate_value_rule("rate_mode", "rate_value"), name="ck_sales_pay_plan_rates_rate_value"),
    )

    id = Column(Integer, primary_key=True)
    plan_version_id = _fk(__tablename__, "plan_version_id", "sales_pay_plan_versions")
    product_id = _fk(__tablename__, "product_id", "products", nullable=True)
    from_unit = Column(Integer, nullable=False)
    rate_mode = Column(String(10), nullable=False)
    rate_value = _money()
    created_at = _created_at()

    version = db.relationship("SalesPayPlanVersion", back_populates="rates")
    product = db.relationship("Product", foreign_keys=[product_id])

    def __repr__(self):
        return f"<SalesPayPlanRate v={self.plan_version_id} product={self.product_id} {self.rate_mode}>"


class SalesAgentPayTerms(db.Model):
    """An agent's base salary and plan from a month onward. Insert-only: a raise is a new row.

    `SalesPayTermsService.terms_for(agent, month)` takes the latest `effective_month <= month`
    (then the highest id), so history is never rewritten.
    """

    __tablename__ = "sales_agent_pay_terms"
    __table_args__ = (
        CheckConstraint("base_salary >= 0", name="ck_sales_agent_pay_terms_base_nonneg"),
        _whole(__tablename__, "base_salary"),
        _first_of_month(__tablename__, "effective_month"),
        Index("ix_sales_agent_pay_terms_agent_month", "agent_user_id", "effective_month"),
    )

    id = Column(Integer, primary_key=True)
    agent_user_id = _fk(__tablename__, "agent_user_id", "users")
    effective_month = Column(Date, nullable=False)
    base_salary = _money()
    plan_id = _fk(__tablename__, "plan_id", "sales_pay_plans")
    note = Column(Text, nullable=True)
    created_by_user_id = _fk(__tablename__, "created_by_user_id", "users")
    created_at = _created_at()

    plan = db.relationship("SalesPayPlan", foreign_keys=[plan_id])
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])

    def __repr__(self):
        return f"<SalesAgentPayTerms agent={self.agent_user_id} from={self.effective_month}>"


class SalesPayPeriod(db.Model, TimestampMixin):
    """One company month, moving open → closed → approved → paid under a row lock.

    A statement has no status of its own; it takes its period's. The stamp CHECKs use the
    portable boolean-equality form (precedent `ck_outlets_coords_pair`): each status carries
    exactly the stamps its transitions wrote.
    """

    __tablename__ = "sales_pay_periods"
    __table_args__ = (
        UniqueConstraint("month_start", name="uq_sales_pay_periods_month_start"),
        CheckConstraint(f"status IN ({_quoted(SALES_PAY_PERIOD_STATUSES)})", name="ck_sales_pay_periods_status"),
        CheckConstraint(
            "(status = 'open') = (closed_at IS NULL) AND (closed_at IS NULL) = (closed_by_user_id IS NULL)",
            name="ck_sales_pay_periods_closed_stamp",
        ),
        CheckConstraint(
            "(status IN ('approved', 'paid')) = (approved_at IS NOT NULL) "
            "AND (approved_at IS NULL) = (approved_by_user_id IS NULL)",
            name="ck_sales_pay_periods_approved_stamp",
        ),
        CheckConstraint(
            "(status = 'paid') = (paid_on IS NOT NULL) AND (paid_on IS NULL) = (paid_recorded_at IS NULL) "
            "AND (paid_on IS NULL) = (paid_by_user_id IS NULL)",
            name="ck_sales_pay_periods_paid_stamp",
        ),
        _first_of_month(__tablename__, "month_start"),
    )

    id = Column(Integer, primary_key=True)
    month_start = Column(Date, nullable=False)
    status = Column(String(20), nullable=False, default="open")
    # True only on the first period, and only when `start` sets it (I-16).
    is_shadow = Column(Boolean, nullable=False, default=False)
    # [{"date": "2026-10-01", "note": "..."}]: inside the month; any day under the seven-day default (C2).
    holidays = Column(JSON, nullable=False, default=list)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    closed_by_user_id = _fk(__tablename__, "closed_by_user_id", "users", nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    approved_by_user_id = _fk(__tablename__, "approved_by_user_id", "users", nullable=True)
    # The date the admin says pay went out, and when and by whom that was recorded.
    paid_on = Column(Date, nullable=True)
    paid_recorded_at = Column(DateTime(timezone=True), nullable=True)
    paid_by_user_id = _fk(__tablename__, "paid_by_user_id", "users", nullable=True)
    last_synced_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_stats = Column(JSON, nullable=True)

    closed_by = db.relationship("User", foreign_keys=[closed_by_user_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_user_id])
    paid_by = db.relationship("User", foreign_keys=[paid_by_user_id])

    def __repr__(self):
        return f"<SalesPayPeriod {self.month_start} {self.status}>"


class SalesAgentUnpaidDay(db.Model):
    """A day an agent is not paid for. Created and deleted while the month is open or closed."""

    __tablename__ = "sales_agent_unpaid_days"
    __table_args__ = (UniqueConstraint("agent_user_id", "unpaid_date", name="uq_sales_agent_unpaid_days_agent_date"),)

    id = Column(Integer, primary_key=True)
    agent_user_id = _fk(__tablename__, "agent_user_id", "users")
    unpaid_date = Column(Date, nullable=False)
    note = Column(Text, nullable=True)
    created_by_user_id = _fk(__tablename__, "created_by_user_id", "users")
    created_at = _created_at()

    created_by = db.relationship("User", foreign_keys=[created_by_user_id])

    def __repr__(self):
        return f"<SalesAgentUnpaidDay agent={self.agent_user_id} {self.unpaid_date}>"


class SalesPayLedgerLine(db.Model):
    """One earning event, strictly append-only. `SalesPayLedgerService.sync` is its only writer.

    Commission lines belong to an order (`agent_user_id` is `orders.created_by_staff_id`); a
    new-outlet bonus belongs to an outlet (its onboarder). Every later line of an order carries
    the first credit's `earned_month` (I-19). Commission lines carry no amount (v5): they record
    the order's level, and the calculator decides the money at month level (§4.7). "Late" is not
    stored: `SalesPayPeriodService.is_late` derives it from `earned_month` and the period.
    `order_id` is a real FK, which is why `OrderDeletionService` refuses to hard-delete a
    credited order (§3.6).
    """

    __tablename__ = "sales_pay_ledger_lines"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_sales_pay_ledger_lines_idempotency_key"),
        # Declared first: an unknown kind also breaks the per-kind rules below, and SQLite
        # names the first CHECK that fails.
        CheckConstraint(f"kind IN ({_quoted(SALES_PAY_LEDGER_KINDS)})", name="ck_sales_pay_ledger_lines_kind"),
        # `cause IS NOT NULL` is spelled out because `NULL IN (...)` is NULL, and a CHECK passes on NULL.
        CheckConstraint(
            "(kind = 'commission_reversal' AND cause IS NOT NULL "
            f"AND cause IN ({_quoted(SALES_PAY_REVERSAL_CAUSES)})) "
            "OR (kind = 'commission_difference' AND cause IS NOT NULL "
            f"AND cause IN ({_quoted(SALES_PAY_DIFFERENCE_CAUSES)})) "
            "OR (kind IN ('commission_credit', 'new_outlet_bonus') AND cause IS NULL)",
            name="ck_sales_pay_ledger_lines_cause",
        ),
        CheckConstraint(
            "(kind = 'new_outlet_bonus') = (outlet_id IS NOT NULL) AND (order_id IS NULL) = (outlet_id IS NOT NULL)",
            name="ck_sales_pay_ledger_lines_subject",
        ),
        # v5: a commission line records a level (§3.2) and never money; a new-outlet bonus is the
        # one line with an amount. A difference's direction is the sync's own comparison of levels
        # (`pay_rules.difference_cause`), so no CHECK restates it.
        CheckConstraint(
            "(kind = 'new_outlet_bonus' AND amount IS NOT NULL AND amount >= 0) "
            "OR (kind <> 'new_outlet_bonus' AND amount IS NULL)",
            name="ck_sales_pay_ledger_lines_amount_kind",
        ),
        _whole(__tablename__, "amount"),
        _first_of_month(__tablename__, "earned_month"),
        Index("ix_sales_pay_ledger_lines_agent_period", "agent_user_id", "period_id"),
        Index("ix_sales_pay_ledger_lines_order_id", "order_id"),
        Index("ix_sales_pay_ledger_lines_outlet_id", "outlet_id"),
    )

    id = Column(Integer, primary_key=True)
    agent_user_id = _fk(__tablename__, "agent_user_id", "users")
    # The OPEN period the line was posted into.
    period_id = _fk(__tablename__, "period_id", "sales_pay_periods")
    kind = Column(String(30), nullable=False)
    cause = Column(String(20), nullable=True)
    order_id = _fk(__tablename__, "order_id", "orders", nullable=True)
    outlet_id = _fk(__tablename__, "outlet_id", "outlets", nullable=True)
    plan_version_id = _fk(__tablename__, "plan_version_id", "sales_pay_plan_versions")
    # A new-outlet bonus: whole UZS, before the gate. NULL on every commission line (v5).
    amount = _money(nullable=True)
    earned_month = Column(Date, nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    # `commission:{order_id}:{seq}` or `new_outlet_bonus:{outlet_id}`.
    idempotency_key = Column(String(64), nullable=False)
    snapshot = Column(JSON, nullable=False)
    created_at = _created_at()

    period = db.relationship("SalesPayPeriod", foreign_keys=[period_id])
    order = db.relationship("Order", foreign_keys=[order_id])
    outlet = db.relationship("Outlet", foreign_keys=[outlet_id])
    plan_version = db.relationship("SalesPayPlanVersion", foreign_keys=[plan_version_id])

    def __repr__(self):
        return f"<SalesPayLedgerLine {self.id} {self.kind} {self.amount} agent={self.agent_user_id}>"


class SalesPayOutletCheck(db.Model, TimestampMixin):
    """The new-outlet evaluation of one outlet, ever. It freezes the one verdict that cannot be
    recomputed later: whether the outlet was already a customer. `order_scope` depends on the
    live branch count, and a sibling outlet added later would flip it.

    Only `tracking` rows change state; every other status is terminal. `window_start` /
    `window_end` are written together, once, at the outlet's first delivery (D-Q12).
    """

    __tablename__ = "sales_pay_outlet_checks"
    __table_args__ = (
        UniqueConstraint("outlet_id", name="uq_sales_pay_outlet_checks_outlet_id"),
        CheckConstraint(
            f"status IN ({_quoted(SALES_PAY_OUTLET_CHECK_STATUSES)})", name="ck_sales_pay_outlet_checks_status"
        ),
        CheckConstraint(
            "(status = 'qualified') = (ledger_line_id IS NOT NULL)", name="ck_sales_pay_outlet_checks_qualified"
        ),
        CheckConstraint(
            "(window_start IS NULL) = (window_end IS NULL) AND (window_end IS NULL OR window_end > window_start)",
            name="ck_sales_pay_outlet_checks_window_pair",
        ),
    )

    id = Column(Integer, primary_key=True)
    outlet_id = _fk(__tablename__, "outlet_id", "outlets")
    # The onboarder.
    agent_user_id = _fk(__tablename__, "agent_user_id", "users")
    status = Column(String(20), nullable=False)
    # The evaluation version (I-9); NULL only for `not_eligible`.
    plan_version_id = _fk(__tablename__, "plan_version_id", "sales_pay_plan_versions", nullable=True)
    activation_reason = Column(String(40), nullable=False)
    activation_actor_user_id = _fk(__tablename__, "activation_actor_user_id", "users", nullable=True)
    activated_at = Column(DateTime(timezone=True), nullable=False)
    onboarded_at = Column(DateTime(timezone=True), nullable=False)
    window_start = Column(DateTime(timezone=True), nullable=True)
    window_end = Column(DateTime(timezone=True), nullable=True)
    ledger_line_id = _fk(__tablename__, "ledger_line_id", "sales_pay_ledger_lines", nullable=True)
    evidence = Column(JSON, nullable=False)

    outlet = db.relationship("Outlet", foreign_keys=[outlet_id])
    plan_version = db.relationship("SalesPayPlanVersion", foreign_keys=[plan_version_id])
    ledger_line = db.relationship("SalesPayLedgerLine", foreign_keys=[ledger_line_id])

    def __repr__(self):
        return f"<SalesPayOutletCheck outlet={self.outlet_id} {self.status}>"


@translatable("name")
class SalesPayPenaltyType(db.Model, TimestampMixin, TranslatableMixin):
    """A kind of penalty with its default amount. Deactivated, never deleted.

    `name` holds the canonical English; uz, ru AND en live in `Translation` rows
    `SalesPayPenaltyType.name.<id>`, which the service writes in full. `default_amount` is a pay
    amount, so it is never sent to managers.
    """

    __tablename__ = "sales_pay_penalty_types"
    __table_args__ = (
        CheckConstraint("default_amount > 0", name="ck_sales_pay_penalty_types_default_amount_positive"),
        _whole(__tablename__, "default_amount"),
    )

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    default_amount = _money()
    is_active = Column(Boolean, nullable=False, default=True)
    created_by_user_id = _fk(__tablename__, "created_by_user_id", "users")
    updated_by_user_id = _fk(__tablename__, "updated_by_user_id", "users", nullable=True)

    def __repr__(self):
        return f"<SalesPayPenaltyType {self.id} {self.name!r} active={self.is_active}>"


class SalesPayPenalty(db.Model, TimestampMixin):
    """A penalty, proposed by a manager or an admin and decided by an admin, or created
    confirmed by an admin (`origin = 'direct'`).

    The CHECKs enforce the state matrix of §3.2:

    | status    | amount, period_id | decided_* | cancelled_* |
    | proposed  | NULL              | NULL      | NULL        |
    | rejected  | NULL              | set       | NULL        |
    | confirmed | set               | set       | NULL        |
    | cancelled | set               | set       | set + reason|
    """

    __tablename__ = "sales_pay_penalties"
    __table_args__ = (
        CheckConstraint(f"status IN ({_quoted(SALES_PAY_PENALTY_STATUSES)})", name="ck_sales_pay_penalties_status"),
        CheckConstraint(f"origin IN ({_quoted(SALES_PAY_PENALTY_ORIGINS)})", name="ck_sales_pay_penalties_origin"),
        CheckConstraint(
            "(status IN ('confirmed', 'cancelled')) = (amount IS NOT NULL AND period_id IS NOT NULL)",
            name="ck_sales_pay_penalties_booked",
        ),
        CheckConstraint(
            "(status = 'proposed') = (decided_at IS NULL) AND (decided_at IS NULL) = (decided_by_user_id IS NULL)",
            name="ck_sales_pay_penalties_decided",
        ),
        CheckConstraint(
            "(status = 'cancelled') = (cancelled_at IS NOT NULL) "
            "AND (cancelled_at IS NULL) = (cancelled_by_user_id IS NULL) "
            "AND (cancelled_at IS NULL OR cancel_reason IS NOT NULL)",
            name="ck_sales_pay_penalties_cancelled",
        ),
        CheckConstraint("amount IS NULL OR amount > 0", name="ck_sales_pay_penalties_amount_positive"),
        _whole(__tablename__, "amount"),
        CheckConstraint(
            "origin <> 'direct' OR status IN ('confirmed', 'cancelled')",
            name="ck_sales_pay_penalties_direct_confirmed",
        ),
        Index("ix_sales_pay_penalties_agent_period", "agent_user_id", "period_id"),
        Index("ix_sales_pay_penalties_status", "status"),
    )

    id = Column(Integer, primary_key=True)
    agent_user_id = _fk(__tablename__, "agent_user_id", "users")
    penalty_type_id = _fk(__tablename__, "penalty_type_id", "sales_pay_penalty_types")
    origin = Column(String(10), nullable=False)
    incident_date = Column(Date, nullable=False)
    reason = Column(Text, nullable=False)
    evidence = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="proposed")
    # Both set at confirmation; the period by `SalesPayPeriodService.route_manual` (I-13).
    amount = _money(nullable=True)
    period_id = _fk(__tablename__, "period_id", "sales_pay_periods", nullable=True)
    proposed_by_user_id = _fk(__tablename__, "proposed_by_user_id", "users")
    proposed_at = Column(DateTime(timezone=True), nullable=False)
    decided_by_user_id = _fk(__tablename__, "decided_by_user_id", "users", nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(Text, nullable=True)
    cancelled_by_user_id = _fk(__tablename__, "cancelled_by_user_id", "users", nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancel_reason = Column(Text, nullable=True)

    penalty_type = db.relationship("SalesPayPenaltyType", foreign_keys=[penalty_type_id])
    period = db.relationship("SalesPayPeriod", foreign_keys=[period_id])
    agent = db.relationship("User", foreign_keys=[agent_user_id])
    proposed_by = db.relationship("User", foreign_keys=[proposed_by_user_id])
    decided_by = db.relationship("User", foreign_keys=[decided_by_user_id])
    cancelled_by = db.relationship("User", foreign_keys=[cancelled_by_user_id])

    def __repr__(self):
        return f"<SalesPayPenalty {self.id} agent={self.agent_user_id} {self.status}>"


class SalesPayAdjustment(db.Model):
    """A signed admin bonus or deduction, or last month's carried shortfall. Strictly
    append-only, with no status: a mistake is corrected by a counter-adjustment with a reason.

    A carry is always a shortfall (C1), and there is at most one per agent from one month:
    `uq_sales_pay_adjustments_carry_from_agent` is the duplicate backstop. Admin rows have
    `carried_from_period_id` NULL, and NULLs are distinct in a unique, so it never reaches them.
    """

    __tablename__ = "sales_pay_adjustments"
    __table_args__ = (
        CheckConstraint("amount <> 0", name="ck_sales_pay_adjustments_amount_nonzero"),
        _whole(__tablename__, "amount"),
        CheckConstraint(f"source IN ({_quoted(SALES_PAY_ADJUSTMENT_SOURCES)})", name="ck_sales_pay_adjustments_source"),
        CheckConstraint(
            "(source = 'carry_forward') = (carried_from_period_id IS NOT NULL)",
            name="ck_sales_pay_adjustments_carry",
        ),
        CheckConstraint("source <> 'carry_forward' OR amount < 0", name="ck_sales_pay_adjustments_carry_sign"),
        UniqueConstraint("carried_from_period_id", "agent_user_id", name="uq_sales_pay_adjustments_carry_from_agent"),
    )

    id = Column(Integer, primary_key=True)
    agent_user_id = _fk(__tablename__, "agent_user_id", "users")
    period_id = _fk(__tablename__, "period_id", "sales_pay_periods")
    amount = _money()
    reason = Column(Text, nullable=False)
    source = Column(String(20), nullable=False)
    carried_from_period_id = _fk(__tablename__, "carried_from_period_id", "sales_pay_periods", nullable=True)
    # For a carry, the approving admin.
    created_by_user_id = _fk(__tablename__, "created_by_user_id", "users")
    created_at = _created_at()

    period = db.relationship("SalesPayPeriod", foreign_keys=[period_id])
    carried_from_period = db.relationship("SalesPayPeriod", foreign_keys=[carried_from_period_id])
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])

    def __repr__(self):
        return f"<SalesPayAdjustment {self.id} agent={self.agent_user_id} {self.amount} {self.source}>"


_STATEMENT_MONEY = (
    "base_salary",
    "base_amount",
    "commission_amount",
    "late_commission_amount",
    "gated_commission_amount",
    "bonus_amount",
    "penalty_amount",
    "adjustment_amount",
    "variable_amount",
    "carry_in_amount",
    "gross_amount",
    "total_amount",
    "carry_out_amount",
    "owed_amount",
)


class SalesPayStatement(db.Model, TimestampMixin):
    """An agent's frozen statement for one period: written at close, or by a re-freeze while
    the period is closed (`revision` counts those).

    `pay_calculator.calculate` is the one expression of the gating formula, the paid-total floor
    and the carry-versus-owed split; the CHECKs below only pin the signs and the sum
    `gross = base + variable + carry_in` (C1). `owed_in_statement_id` names the statement whose
    owed balance this one nets (I-28, Q15). The earlier statement is never updated, and
    `uq_sales_pay_statements_owed_in_statement_id` lets a balance be netted at most once.
    """

    __tablename__ = "sales_pay_statements"
    __table_args__ = (
        UniqueConstraint("period_id", "agent_user_id", name="uq_sales_pay_statements_period_agent"),
        UniqueConstraint("owed_in_statement_id", name="uq_sales_pay_statements_owed_in_statement_id"),
        _whole(__tablename__, *_STATEMENT_MONEY),
        CheckConstraint(
            "base_salary >= 0 AND base_amount >= 0 AND bonus_amount >= 0 AND penalty_amount >= 0 "
            "AND carry_in_amount <= 0 AND total_amount >= 0 AND carry_out_amount <= 0 AND owed_amount >= 0",
            name="ck_sales_pay_statements_nonneg",
        ),
        CheckConstraint(
            "gross_amount = base_amount + variable_amount + carry_in_amount", name="ck_sales_pay_statements_total"
        ),
        CheckConstraint("owed_in_statement_id IS NULL OR carry_in_amount < 0", name="ck_sales_pay_statements_owed_in"),
        CheckConstraint(
            "working_days >= 0 AND worked_days BETWEEN 0 AND working_days", name="ck_sales_pay_statements_days"
        ),
        CheckConstraint(
            "gate_multiplier BETWEEN 0 AND 1 AND gate_compliance_pct BETWEEN 0 AND 100 AND revision >= 1",
            name="ck_sales_pay_statements_gate",
        ),
    )

    id = Column(Integer, primary_key=True)
    period_id = _fk(__tablename__, "period_id", "sales_pay_periods")
    agent_user_id = _fk(__tablename__, "agent_user_id", "users")
    # Close refuses when terms are missing (§4.9), so a statement always has them.
    terms_id = _fk(__tablename__, "terms_id", "sales_agent_pay_terms")
    plan_version_id = _fk(__tablename__, "plan_version_id", "sales_pay_plan_versions")
    revision = Column(Integer, nullable=False, default=1)
    computed_at = Column(DateTime(timezone=True), nullable=False)
    computed_by_user_id = _fk(__tablename__, "computed_by_user_id", "users")
    base_salary = _money()
    working_days = Column(Integer, nullable=False)
    worked_days = Column(Integer, nullable=False)
    base_amount = _money()
    commission_amount = _money()
    late_commission_amount = _money()
    gate_visits_due = Column(Integer, nullable=False)
    gate_visits_counted = Column(Integer, nullable=False)
    # NULL when nothing was due.
    gate_compliance_pct = Column(Numeric(5, 1), nullable=True)
    # This month's multiplier; later months read it for this month's late lines (§4.7).
    gate_multiplier = Column(Numeric(4, 3), nullable=False)
    gated_commission_amount = _money()
    bonus_amount = _money()
    penalty_amount = _money()
    adjustment_amount = _money()
    variable_amount = _money()
    carry_in_amount = _money()
    owed_in_statement_id = _fk(__tablename__, "owed_in_statement_id", "sales_pay_statements", nullable=True)
    gross_amount = _money()
    # The paid total: max(0, gross), the only floor (C1).
    total_amount = _money()
    carry_out_amount = _money()
    owed_amount = _money()
    inputs = Column(JSON, nullable=False)

    period = db.relationship("SalesPayPeriod", foreign_keys=[period_id])
    agent = db.relationship("User", foreign_keys=[agent_user_id])
    terms = db.relationship("SalesAgentPayTerms", foreign_keys=[terms_id])
    plan_version = db.relationship("SalesPayPlanVersion", foreign_keys=[plan_version_id])
    owed_in_statement = db.relationship(
        "SalesPayStatement", foreign_keys=[owed_in_statement_id], remote_side="SalesPayStatement.id"
    )

    def __repr__(self):
        return f"<SalesPayStatement period={self.period_id} agent={self.agent_user_id} r{self.revision}>"
