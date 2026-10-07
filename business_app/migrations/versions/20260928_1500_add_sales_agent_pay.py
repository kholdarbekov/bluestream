"""add sales-agent pay: plans, terms, periods, earnings ledger, outlet checks, penalties,
adjustments, statements; employment dates; frozen due sets; orders attribution index;
same-day agent order approval holds

Revision ID: b7e3c1a95d42
Revises: a4c7e9b2d5f8
Create Date: 2026-09-28 15:00:00.000000

Spec: docs/superpowers/specs/2026-09-28-sales-agent-compensation-design.md §3.
Hand-written so every constraint is named (CONVENTIONS.md §5).

Tables added (13): sales_pay_plans, sales_pay_plan_versions, sales_pay_plan_rates,
sales_agent_pay_terms, sales_pay_periods, sales_agent_unpaid_days, sales_pay_ledger_lines,
sales_pay_outlet_checks, sales_pay_penalty_types, sales_pay_penalties, sales_pay_adjustments,
sales_pay_statements, and agent_order_approvals (the same-day approval hold, C14; not a pay table).
Existing tables: sales_agent_profiles + employment_start_date / employment_end_date /
employment_set_by_user_id / employment_set_at (+ 2 ck, 1 fk);
sales_agent_day_plans + due_outlet_ids; orders + ix_orders_order_source_created_by_staff_id.
Postgres-only: the first-of-month CHECKs. No enum types, no triggers, no seed rows.

Rollback strategy:
    downgrade() drops the 13 tables (children first), the orders index, due_outlet_ids and the
    four profile columns. DESTRUCTIVE once any month has been closed: frozen earned instants,
    late placements, approved statements, penalty decisions and plan/terms history cannot be
    recomputed because orders keep changing afterwards. Before downgrading an environment that
    has closed a month, pg_dump every sales_pay_* table plus sales_agent_pay_terms,
    sales_agent_unpaid_days, sales_agent_day_plans and agent_order_approvals. Nothing outside
    this feature references the new tables.
    Pending rows in agent_order_approvals must be decided before a downgrade or a code rollback;
    old code does not see the table and confirms those orders within 10 minutes (spec R30).
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b7e3c1a95d42"
down_revision = "a4c7e9b2d5f8"
branch_labels = None
depends_on = None

# Frozen copies of shared/staff_constants.py SALES_PAY_* (migrations never import live tuples).
PERIOD_STATUSES = ("open", "closed", "approved", "paid")
RATE_MODES = ("per_unit", "percent")
LEDGER_KINDS = ("commission_credit", "commission_reversal", "commission_difference", "new_outlet_bonus")
PENALTY_STATUSES = ("proposed", "confirmed", "rejected", "cancelled")
PENALTY_ORIGINS = ("proposal", "direct")
ADJUSTMENT_SOURCES = ("admin", "carry_forward")
CHECK_STATUSES = ("tracking", "qualified", "prior_customer", "not_eligible", "expired")
REVERSAL_CAUSES = ("not_delivered", "nothing_received")
DIFFERENCE_CAUSES = ("received_reduced", "received_restored", "plan_changed")
# models/sales_visits.AGENT_ORDER_APPROVAL_STATUSES
APPROVAL_STATUSES = ("pending", "approved", "rejected", "cancelled")

STATEMENT_MONEY = (
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

DROP_ORDER = (
    "agent_order_approvals",
    "sales_pay_statements",
    "sales_pay_adjustments",
    "sales_pay_penalties",
    "sales_pay_penalty_types",
    "sales_pay_outlet_checks",
    "sales_pay_ledger_lines",
    "sales_agent_unpaid_days",
    "sales_pay_periods",
    "sales_agent_pay_terms",
    "sales_pay_plan_rates",
    "sales_pay_plan_versions",
    "sales_pay_plans",
)


def _quoted(values):
    return ", ".join(f"'{v}'" for v in values)


def _fk(table, col, target, nullable=False):
    return sa.Column(col, sa.Integer(), sa.ForeignKey(f"{target}.id", name=f"fk_{table}_{col}"), nullable=nullable)


def _money(col, nullable=False):
    return sa.Column(col, sa.Numeric(14, 2), nullable=nullable)


def _ts(col, nullable=False):
    return sa.Column(col, sa.DateTime(timezone=True), nullable=nullable)


def _whole(table, *cols):
    name = f"ck_{table}_{cols[0]}_whole" if len(cols) == 1 else f"ck_{table}_amounts_whole"
    return sa.CheckConstraint(" AND ".join(f"{c} = ROUND({c}, 0)" for c in cols), name=name)


def _first_of_month(table, col):
    return sa.CheckConstraint(f"EXTRACT(DAY FROM {col}) = 1", name=f"ck_{table}_{col}_first_of_month")


def _rate_value_rule(mode, value):
    return (
        f"{value} >= 0 AND ({mode} <> 'percent' OR {value} <= 100) "
        f"AND ({mode} <> 'per_unit' OR {value} = ROUND({value}, 0))"
    )


def upgrade():
    pg = op.get_bind().dialect.name == "postgresql"

    def month_check(table, col):
        return [_first_of_month(table, col)] if pg else []

    t = "sales_pay_plans"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        _fk(t, "created_by_user_id", "users"),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("name", name="uq_sales_pay_plans_name"),
    )

    t = "sales_pay_plan_versions"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), primary_key=True),
        _fk(t, "plan_id", "sales_pay_plans"),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("effective_month", sa.Date(), nullable=False),
        sa.Column("default_rate_mode", sa.String(10), nullable=False),
        _money("default_rate_value"),
        sa.Column("gate_bands", sa.JSON(), nullable=False),
        sa.Column("gate_min_visits_due", sa.Integer(), nullable=False),
        _money("bonus_amount"),
        sa.Column("bonus_window_days", sa.Integer(), nullable=False),
        sa.Column("bonus_min_orders_with_total", sa.Integer(), nullable=False),
        _money("bonus_min_combined_total"),
        sa.Column("bonus_min_orders_any_amount", sa.Integer(), nullable=False),
        sa.Column("bonus_prior_customer_lookback_days", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        _fk(t, "created_by_user_id", "users"),
        _ts("created_at"),
        sa.UniqueConstraint("plan_id", "version_no", name="uq_sales_pay_plan_versions_plan_version"),
        sa.CheckConstraint(
            f"default_rate_mode IN ({_quoted(RATE_MODES)})", name="ck_sales_pay_plan_versions_default_rate_mode"
        ),
        sa.CheckConstraint(
            _rate_value_rule("default_rate_mode", "default_rate_value"),
            name="ck_sales_pay_plan_versions_default_rate_value",
        ),
        sa.CheckConstraint(
            "gate_min_visits_due >= 0 AND bonus_amount >= 0 AND bonus_window_days >= 1 "
            "AND bonus_min_orders_with_total >= 1 AND bonus_min_combined_total >= 0 "
            "AND bonus_min_orders_any_amount >= 1 AND bonus_prior_customer_lookback_days >= 0 "
            "AND version_no >= 1",
            name="ck_sales_pay_plan_versions_ranges",
        ),
        _whole(t, "bonus_amount", "bonus_min_combined_total"),
        *month_check(t, "effective_month"),
    )
    op.create_index("ix_sales_pay_plan_versions_plan_month", t, ["plan_id", "effective_month"])

    t = "sales_pay_plan_rates"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), primary_key=True),
        _fk(t, "plan_version_id", "sales_pay_plan_versions"),
        _fk(t, "product_id", "products"),
        sa.Column("rate_mode", sa.String(10), nullable=False),
        _money("rate_value"),
        _ts("created_at"),
        sa.UniqueConstraint("plan_version_id", "product_id", name="uq_sales_pay_plan_rates_version_product"),
        sa.CheckConstraint(f"rate_mode IN ({_quoted(RATE_MODES)})", name="ck_sales_pay_plan_rates_rate_mode"),
        sa.CheckConstraint(_rate_value_rule("rate_mode", "rate_value"), name="ck_sales_pay_plan_rates_rate_value"),
    )

    t = "sales_agent_pay_terms"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), primary_key=True),
        _fk(t, "agent_user_id", "users"),
        sa.Column("effective_month", sa.Date(), nullable=False),
        _money("base_salary"),
        _fk(t, "plan_id", "sales_pay_plans"),
        sa.Column("note", sa.Text(), nullable=True),
        _fk(t, "created_by_user_id", "users"),
        _ts("created_at"),
        sa.CheckConstraint("base_salary >= 0", name="ck_sales_agent_pay_terms_base_nonneg"),
        _whole(t, "base_salary"),
        *month_check(t, "effective_month"),
    )
    op.create_index("ix_sales_agent_pay_terms_agent_month", t, ["agent_user_id", "effective_month"])

    t = "sales_pay_periods"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("month_start", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("is_shadow", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("holidays", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        _ts("closed_at", nullable=True),
        _fk(t, "closed_by_user_id", "users", nullable=True),
        _ts("approved_at", nullable=True),
        _fk(t, "approved_by_user_id", "users", nullable=True),
        sa.Column("paid_on", sa.Date(), nullable=True),
        _ts("paid_recorded_at", nullable=True),
        _fk(t, "paid_by_user_id", "users", nullable=True),
        _ts("last_synced_at", nullable=True),
        sa.Column("last_sync_stats", sa.JSON(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("month_start", name="uq_sales_pay_periods_month_start"),
        sa.CheckConstraint(f"status IN ({_quoted(PERIOD_STATUSES)})", name="ck_sales_pay_periods_status"),
        sa.CheckConstraint(
            "(status = 'open') = (closed_at IS NULL) AND (closed_at IS NULL) = (closed_by_user_id IS NULL)",
            name="ck_sales_pay_periods_closed_stamp",
        ),
        sa.CheckConstraint(
            "(status IN ('approved', 'paid')) = (approved_at IS NOT NULL) "
            "AND (approved_at IS NULL) = (approved_by_user_id IS NULL)",
            name="ck_sales_pay_periods_approved_stamp",
        ),
        sa.CheckConstraint(
            "(status = 'paid') = (paid_on IS NOT NULL) AND (paid_on IS NULL) = (paid_recorded_at IS NULL) "
            "AND (paid_on IS NULL) = (paid_by_user_id IS NULL)",
            name="ck_sales_pay_periods_paid_stamp",
        ),
        *month_check(t, "month_start"),
    )

    t = "sales_agent_unpaid_days"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), primary_key=True),
        _fk(t, "agent_user_id", "users"),
        sa.Column("unpaid_date", sa.Date(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        _fk(t, "created_by_user_id", "users"),
        _ts("created_at"),
        sa.UniqueConstraint("agent_user_id", "unpaid_date", name="uq_sales_agent_unpaid_days_agent_date"),
    )

    t = "sales_pay_ledger_lines"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), primary_key=True),
        _fk(t, "agent_user_id", "users"),
        _fk(t, "period_id", "sales_pay_periods"),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("cause", sa.String(20), nullable=True),
        _fk(t, "order_id", "orders", nullable=True),
        _fk(t, "outlet_id", "outlets", nullable=True),
        _fk(t, "plan_version_id", "sales_pay_plan_versions"),
        _money("amount"),
        sa.Column("earned_month", sa.Date(), nullable=False),
        _ts("occurred_at"),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("idempotency_key", name="uq_sales_pay_ledger_lines_idempotency_key"),
        sa.CheckConstraint(f"kind IN ({_quoted(LEDGER_KINDS)})", name="ck_sales_pay_ledger_lines_kind"),
        # `cause IS NOT NULL`: `NULL IN (...)` is NULL, and a CHECK passes on NULL.
        sa.CheckConstraint(
            "(kind = 'commission_reversal' AND cause IS NOT NULL "
            f"AND cause IN ({_quoted(REVERSAL_CAUSES)})) "
            "OR (kind = 'commission_difference' AND cause IS NOT NULL "
            f"AND cause IN ({_quoted(DIFFERENCE_CAUSES)})) "
            "OR (kind IN ('commission_credit', 'new_outlet_bonus') AND cause IS NULL)",
            name="ck_sales_pay_ledger_lines_cause",
        ),
        sa.CheckConstraint(
            "(kind = 'new_outlet_bonus') = (outlet_id IS NOT NULL) AND (order_id IS NULL) = (outlet_id IS NOT NULL)",
            name="ck_sales_pay_ledger_lines_subject",
        ),
        sa.CheckConstraint(
            "(kind = 'commission_credit' AND amount >= 0) "
            "OR (kind = 'commission_reversal' AND amount <= 0) "
            "OR (kind = 'commission_difference' AND cause = 'received_reduced' AND amount < 0) "
            "OR (kind = 'commission_difference' AND cause = 'received_restored' AND amount > 0) "
            "OR (kind = 'commission_difference' AND cause = 'plan_changed' AND amount <> 0) "
            "OR (kind = 'new_outlet_bonus' AND amount >= 0)",
            name="ck_sales_pay_ledger_lines_amount_sign",
        ),
        _whole(t, "amount"),
        *month_check(t, "earned_month"),
    )
    op.create_index("ix_sales_pay_ledger_lines_agent_period", t, ["agent_user_id", "period_id"])
    op.create_index("ix_sales_pay_ledger_lines_order_id", t, ["order_id"])
    op.create_index("ix_sales_pay_ledger_lines_outlet_id", t, ["outlet_id"])

    t = "sales_pay_outlet_checks"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), primary_key=True),
        _fk(t, "outlet_id", "outlets"),
        _fk(t, "agent_user_id", "users"),
        sa.Column("status", sa.String(20), nullable=False),
        _fk(t, "plan_version_id", "sales_pay_plan_versions", nullable=True),
        sa.Column("activation_reason", sa.String(40), nullable=False),
        _fk(t, "activation_actor_user_id", "users", nullable=True),
        _ts("activated_at"),
        _ts("onboarded_at"),
        _ts("window_start", nullable=True),
        _ts("window_end", nullable=True),
        _fk(t, "ledger_line_id", "sales_pay_ledger_lines", nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("outlet_id", name="uq_sales_pay_outlet_checks_outlet_id"),
        sa.CheckConstraint(f"status IN ({_quoted(CHECK_STATUSES)})", name="ck_sales_pay_outlet_checks_status"),
        sa.CheckConstraint(
            "(status = 'qualified') = (ledger_line_id IS NOT NULL)", name="ck_sales_pay_outlet_checks_qualified"
        ),
        sa.CheckConstraint(
            "(window_start IS NULL) = (window_end IS NULL) AND (window_end IS NULL OR window_end > window_start)",
            name="ck_sales_pay_outlet_checks_window_pair",
        ),
    )

    t = "sales_pay_penalty_types"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        _money("default_amount"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        _fk(t, "created_by_user_id", "users"),
        _fk(t, "updated_by_user_id", "users", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint("default_amount > 0", name="ck_sales_pay_penalty_types_default_amount_positive"),
        _whole(t, "default_amount"),
    )

    t = "sales_pay_penalties"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), primary_key=True),
        _fk(t, "agent_user_id", "users"),
        _fk(t, "penalty_type_id", "sales_pay_penalty_types"),
        sa.Column("origin", sa.String(10), nullable=False),
        sa.Column("incident_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="proposed"),
        _money("amount", nullable=True),
        _fk(t, "period_id", "sales_pay_periods", nullable=True),
        _fk(t, "proposed_by_user_id", "users"),
        _ts("proposed_at"),
        _fk(t, "decided_by_user_id", "users", nullable=True),
        _ts("decided_at", nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        _fk(t, "cancelled_by_user_id", "users", nullable=True),
        _ts("cancelled_at", nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(f"status IN ({_quoted(PENALTY_STATUSES)})", name="ck_sales_pay_penalties_status"),
        sa.CheckConstraint(f"origin IN ({_quoted(PENALTY_ORIGINS)})", name="ck_sales_pay_penalties_origin"),
        sa.CheckConstraint(
            "(status IN ('confirmed', 'cancelled')) = (amount IS NOT NULL AND period_id IS NOT NULL)",
            name="ck_sales_pay_penalties_booked",
        ),
        sa.CheckConstraint(
            "(status = 'proposed') = (decided_at IS NULL) AND (decided_at IS NULL) = (decided_by_user_id IS NULL)",
            name="ck_sales_pay_penalties_decided",
        ),
        sa.CheckConstraint(
            "(status = 'cancelled') = (cancelled_at IS NOT NULL) "
            "AND (cancelled_at IS NULL) = (cancelled_by_user_id IS NULL) "
            "AND (cancelled_at IS NULL OR cancel_reason IS NOT NULL)",
            name="ck_sales_pay_penalties_cancelled",
        ),
        sa.CheckConstraint("amount IS NULL OR amount > 0", name="ck_sales_pay_penalties_amount_positive"),
        _whole(t, "amount"),
        sa.CheckConstraint(
            "origin <> 'direct' OR status IN ('confirmed', 'cancelled')",
            name="ck_sales_pay_penalties_direct_confirmed",
        ),
    )
    op.create_index("ix_sales_pay_penalties_agent_period", t, ["agent_user_id", "period_id"])
    op.create_index("ix_sales_pay_penalties_status", t, ["status"])

    t = "sales_pay_adjustments"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), primary_key=True),
        _fk(t, "agent_user_id", "users"),
        _fk(t, "period_id", "sales_pay_periods"),
        _money("amount"),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        _fk(t, "carried_from_period_id", "sales_pay_periods", nullable=True),
        _fk(t, "created_by_user_id", "users"),
        _ts("created_at"),
        sa.CheckConstraint("amount <> 0", name="ck_sales_pay_adjustments_amount_nonzero"),
        _whole(t, "amount"),
        sa.CheckConstraint(f"source IN ({_quoted(ADJUSTMENT_SOURCES)})", name="ck_sales_pay_adjustments_source"),
        sa.CheckConstraint(
            "(source = 'carry_forward') = (carried_from_period_id IS NOT NULL)",
            name="ck_sales_pay_adjustments_carry",
        ),
        sa.CheckConstraint("source <> 'carry_forward' OR amount < 0", name="ck_sales_pay_adjustments_carry_sign"),
        sa.UniqueConstraint(
            "carried_from_period_id", "agent_user_id", name="uq_sales_pay_adjustments_carry_from_agent"
        ),
    )

    t = "sales_pay_statements"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), primary_key=True),
        _fk(t, "period_id", "sales_pay_periods"),
        _fk(t, "agent_user_id", "users"),
        _fk(t, "terms_id", "sales_agent_pay_terms"),
        _fk(t, "plan_version_id", "sales_pay_plan_versions"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        _ts("computed_at"),
        _fk(t, "computed_by_user_id", "users"),
        _money("base_salary"),
        sa.Column("working_days", sa.Integer(), nullable=False),
        sa.Column("worked_days", sa.Integer(), nullable=False),
        _money("base_amount"),
        _money("commission_amount"),
        _money("late_commission_amount"),
        sa.Column("gate_visits_due", sa.Integer(), nullable=False),
        sa.Column("gate_visits_counted", sa.Integer(), nullable=False),
        sa.Column("gate_compliance_pct", sa.Numeric(5, 1), nullable=True),
        sa.Column("gate_multiplier", sa.Numeric(4, 3), nullable=False),
        _money("gated_commission_amount"),
        _money("bonus_amount"),
        _money("penalty_amount"),
        _money("adjustment_amount"),
        _money("variable_amount"),
        _money("carry_in_amount"),
        _fk(t, "owed_in_statement_id", "sales_pay_statements", nullable=True),
        _money("gross_amount"),
        _money("total_amount"),
        _money("carry_out_amount"),
        _money("owed_amount"),
        sa.Column("inputs", sa.JSON(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("period_id", "agent_user_id", name="uq_sales_pay_statements_period_agent"),
        sa.UniqueConstraint("owed_in_statement_id", name="uq_sales_pay_statements_owed_in_statement_id"),
        _whole(t, *STATEMENT_MONEY),
        sa.CheckConstraint(
            "base_salary >= 0 AND base_amount >= 0 AND bonus_amount >= 0 AND penalty_amount >= 0 "
            "AND carry_in_amount <= 0 AND total_amount >= 0 AND carry_out_amount <= 0 AND owed_amount >= 0",
            name="ck_sales_pay_statements_nonneg",
        ),
        sa.CheckConstraint(
            "gross_amount = base_amount + variable_amount + carry_in_amount", name="ck_sales_pay_statements_total"
        ),
        sa.CheckConstraint(
            "owed_in_statement_id IS NULL OR carry_in_amount < 0", name="ck_sales_pay_statements_owed_in"
        ),
        sa.CheckConstraint(
            "working_days >= 0 AND worked_days BETWEEN 0 AND working_days", name="ck_sales_pay_statements_days"
        ),
        sa.CheckConstraint(
            "gate_multiplier BETWEEN 0 AND 1 AND gate_compliance_pct BETWEEN 0 AND 100 AND revision >= 1",
            name="ck_sales_pay_statements_gate",
        ),
    )

    t = "agent_order_approvals"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), primary_key=True),
        _fk(t, "order_id", "orders"),
        _fk(t, "outlet_id", "outlets"),
        _fk(t, "agent_user_id", "users"),
        sa.Column("earlier_order_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        _ts("requested_at"),
        _ts("decided_at", nullable=True),
        _fk(t, "decided_by_user_id", "users", nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("order_id", name="uq_agent_order_approvals_order_id"),
        sa.CheckConstraint(f"status IN ({_quoted(APPROVAL_STATUSES)})", name="ck_agent_order_approvals_status"),
        sa.CheckConstraint("(status = 'pending') = (decided_at IS NULL)", name="ck_agent_order_approvals_decided"),
        sa.CheckConstraint(
            "(status NOT IN ('approved', 'rejected') OR decided_by_user_id IS NOT NULL) "
            "AND (status <> 'pending' OR decided_by_user_id IS NULL)",
            name="ck_agent_order_approvals_decider",
        ),
        sa.CheckConstraint("(status = 'rejected') = (reason IS NOT NULL)", name="ck_agent_order_approvals_reason"),
    )
    op.create_index("ix_agent_order_approvals_status_requested_at", t, ["status", "requested_at"])
    op.create_index("ix_agent_order_approvals_agent_user_id", t, ["agent_user_id"])

    op.add_column("sales_agent_profiles", sa.Column("employment_start_date", sa.Date(), nullable=True))
    op.add_column("sales_agent_profiles", sa.Column("employment_end_date", sa.Date(), nullable=True))
    op.create_check_constraint(
        "ck_sales_agent_profiles_employment_dates_order",
        "sales_agent_profiles",
        "employment_end_date IS NULL OR employment_start_date IS NULL "
        "OR employment_end_date >= employment_start_date",
    )
    op.add_column("sales_agent_profiles", sa.Column("employment_set_by_user_id", sa.Integer(), nullable=True))
    op.add_column("sales_agent_profiles", sa.Column("employment_set_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_sales_agent_profiles_employment_set_by_user_id",
        "sales_agent_profiles",
        "users",
        ["employment_set_by_user_id"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_sales_agent_profiles_employment_set_pair",
        "sales_agent_profiles",
        "(employment_set_at IS NULL) = (employment_set_by_user_id IS NULL)",
    )
    op.add_column("sales_agent_day_plans", sa.Column("due_outlet_ids", sa.JSON(), nullable=True))
    op.create_index("ix_orders_order_source_created_by_staff_id", "orders", ["order_source", "created_by_staff_id"])


def downgrade():
    op.drop_index("ix_orders_order_source_created_by_staff_id", table_name="orders")
    op.drop_column("sales_agent_day_plans", "due_outlet_ids")
    op.drop_constraint("ck_sales_agent_profiles_employment_set_pair", "sales_agent_profiles", type_="check")
    op.drop_constraint("fk_sales_agent_profiles_employment_set_by_user_id", "sales_agent_profiles", type_="foreignkey")
    op.drop_column("sales_agent_profiles", "employment_set_at")
    op.drop_column("sales_agent_profiles", "employment_set_by_user_id")
    op.drop_constraint("ck_sales_agent_profiles_employment_dates_order", "sales_agent_profiles", type_="check")
    op.drop_column("sales_agent_profiles", "employment_end_date")
    op.drop_column("sales_agent_profiles", "employment_start_date")
    for table in DROP_ORDER:
        op.drop_table(table)
