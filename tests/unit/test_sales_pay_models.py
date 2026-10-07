"""Every portable constraint of the pay tables and the same-day hold, proven on SQLite.

The SQLite suite builds its schema from the models (`db.create_all()`), so these tests prove
the MODEL's constraints. tests/integration/test_sales_pay_pg_constraints.py proves that the
migration carries the same names and columns, and it proves the first-of-month CHECKs, which
exist only on Postgres (`ddl_if(dialect="postgresql")`).

Each refused row starts from a row the schema accepts (`VALID`, itself proven accepted below)
and changes the fewest fields that break exactly one constraint. SQLite names only the FIRST
failing CHECK in declaration order, so a probe that broke two constraints would pass or fail
depending on which one happened to be declared first. The one unavoidable exception is an
unknown ledger kind, which also breaks the per-kind cause and amount rules; see its case.

Completeness is enforced, not hoped for: every CHECK and every unique the models declare must
appear in a case below, so a constraint added later without a proof fails here.

SQLite runs with foreign keys OFF, so a CHECK probe may point an id at nothing. FK
enforcement is proven on Postgres.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import CheckConstraint, PrimaryKeyConstraint, UniqueConstraint
from sqlalchemy.exc import IntegrityError

from business_app.models.order import Order
from business_app.models.sales import Outlet, SalesAgentProfile
from business_app.models.sales_pay import (
    SalesAgentPayTerms,
    SalesAgentUnpaidDay,
    SalesPayAdjustment,
    SalesPayLedgerLine,
    SalesPayOutletCheck,
    SalesPayPenalty,
    SalesPayPenaltyType,
    SalesPayPeriod,
    SalesPayPlan,
    SalesPayPlanRate,
    SalesPayPlanVersion,
    SalesPayStatement,
)
from business_app.models.sales_visits import AgentOrderApproval
from business_app.models.user import User
from shared.enums import OrderStatus, PaymentMethod

OCT = date(2026, 10, 1)
NOV = date(2026, 11, 1)
DEC = date(2026, 12, 1)
ONBOARDED = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
AT = datetime(2026, 10, 4, 9, 12, tzinfo=UTC)
LATER = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
DANGLING_ID = 9001  # FKs are off on SQLite; a CHECK probe only needs the column non-NULL

PAY_MODELS = (
    SalesPayPlan,
    SalesPayPlanVersion,
    SalesPayPlanRate,
    SalesAgentPayTerms,
    SalesPayPeriod,
    SalesAgentUnpaidDay,
    SalesPayLedgerLine,
    SalesPayOutletCheck,
    SalesPayPenaltyType,
    SalesPayPenalty,
    SalesPayAdjustment,
    SalesPayStatement,
    AgentOrderApproval,
)


def _version_fields(plan_id, admin_id, *, version_no, effective_month):
    return {
        "plan_id": plan_id,
        "version_no": version_no,
        "effective_month": effective_month,
        "gate_bands": [{"min_pct": "80.0", "multiplier": "1.000"}, {"min_pct": "0.0", "multiplier": "0.500"}],
        "gate_min_visits_due": 20,
        "bonus_amount": Decimal("300000"),
        "bonus_window_days": 60,
        "bonus_min_orders_with_total": 2,
        "bonus_min_combined_total": Decimal("500000"),
        "bonus_min_orders_any_amount": 5,
        "bonus_prior_customer_lookback_days": 180,
        "created_by_user_id": admin_id,
    }


def _credit_fields(w, *, key_seq):
    return {
        "agent_user_id": w.agent_id,
        "period_id": w.october_id,
        "kind": "commission_credit",
        "cause": None,
        "order_id": w.order_id,
        "outlet_id": None,
        "plan_version_id": w.version_id,
        "amount": None,  # v5: a commission line records a level, never money
        "earned_month": OCT,
        "occurred_at": AT,
        "idempotency_key": f"commission:{w.order_id}:{key_seq}",
        "snapshot": {"order_number": "SA_000301_26"},
    }


@pytest.fixture
def world(db, sample_user, sample_product):
    """The parent rows every probe points at, committed so a probe's rollback keeps them."""
    admin = sample_user
    agent = User(phone="+998900000301", password_hash="not-a-real-hash", first_name="Aziz", last_name="Agent")
    outlet = Outlet(name="Corner Shop", outlet_type="grocery_store", stage="active")
    order = Order(
        user_id=admin.id,
        order_number="SA_000301_26",
        status=OrderStatus.PENDING,
        subtotal=Decimal("120000.00"),
        delivery_fee=Decimal("0.00"),
        total_amount=Decimal("120000.00"),
        payment_method=PaymentMethod.CASH,
        created_at=AT,
    )
    plan = SalesPayPlan(name="Standard", created_by_user_id=admin.id)
    db.session.add_all([agent, outlet, order, plan])
    db.session.flush()

    version = SalesPayPlanVersion(**_version_fields(plan.id, admin.id, version_no=1, effective_month=OCT))
    october = SalesPayPeriod(month_start=OCT, status="open", is_shadow=False, holidays=[])
    november = SalesPayPeriod(month_start=NOV, status="open", is_shadow=False, holidays=[])
    penalty_type = SalesPayPenaltyType(
        name="Missed report", default_amount=Decimal("50000"), is_active=True, created_by_user_id=admin.id
    )
    db.session.add_all([version, october, november, penalty_type])
    db.session.flush()

    terms = SalesAgentPayTerms(
        agent_user_id=agent.id,
        effective_month=OCT,
        base_salary=Decimal("2600000"),
        plan_id=plan.id,
        created_by_user_id=admin.id,
    )
    db.session.add(terms)
    db.session.flush()

    w = SimpleNamespace(
        admin_id=admin.id,
        agent_id=agent.id,
        product_id=sample_product.id,
        outlet_id=outlet.id,
        order_id=order.id,
        plan_id=plan.id,
        version_id=version.id,
        october_id=october.id,
        november_id=november.id,
        penalty_type_id=penalty_type.id,
        terms_id=terms.id,
    )
    line = SalesPayLedgerLine(**_credit_fields(w, key_seq=1))
    db.session.add(line)
    db.session.commit()
    w.ledger_line_id = line.id
    return w


# A row each table accepts. Every probe below starts here.
VALID = {
    SalesPayPlan: lambda w: {"name": "Probe plan", "created_by_user_id": w.admin_id},
    SalesPayPlanVersion: lambda w: _version_fields(w.plan_id, w.admin_id, version_no=2, effective_month=NOV),
    SalesPayPlanRate: lambda w: {
        "plan_version_id": w.version_id,
        "product_id": w.product_id,
        "from_unit": 1,
        "rate_mode": "per_unit",
        "rate_value": Decimal("1500"),
    },
    SalesAgentPayTerms: lambda w: {
        "agent_user_id": w.agent_id,
        "effective_month": NOV,
        "base_salary": Decimal("2600000"),
        "plan_id": w.plan_id,
        "created_by_user_id": w.admin_id,
    },
    SalesPayPeriod: lambda w: {"month_start": DEC, "status": "open", "is_shadow": False, "holidays": []},
    SalesAgentUnpaidDay: lambda w: {
        "agent_user_id": w.agent_id,
        "unpaid_date": date(2026, 10, 7),
        "created_by_user_id": w.admin_id,
    },
    SalesPayLedgerLine: lambda w: _credit_fields(w, key_seq=2),
    SalesPayOutletCheck: lambda w: {
        "outlet_id": w.outlet_id,
        "agent_user_id": w.agent_id,
        "status": "tracking",
        "plan_version_id": w.version_id,
        "activation_reason": "first_order",
        "activated_at": AT,
        "onboarded_at": ONBOARDED,
        "evidence": {},
    },
    SalesPayPenaltyType: lambda w: {
        "name": "Late to the depot",
        "default_amount": Decimal("50000"),
        "is_active": True,
        "created_by_user_id": w.admin_id,
    },
    SalesPayPenalty: lambda w: {
        "agent_user_id": w.agent_id,
        "penalty_type_id": w.penalty_type_id,
        "origin": "proposal",
        "incident_date": date(2026, 10, 6),
        "reason": "Missed the weekly report",
        "evidence": "No report in the group chat on Friday",
        "status": "proposed",
        "proposed_by_user_id": w.admin_id,
        "proposed_at": AT,
    },
    SalesPayAdjustment: lambda w: {
        "agent_user_id": w.agent_id,
        "period_id": w.november_id,
        "amount": Decimal("-50000"),
        "reason": "Share of the van repair",
        "source": "admin",
        "created_by_user_id": w.admin_id,
    },
    SalesPayStatement: lambda w: {
        "period_id": w.october_id,
        "agent_user_id": w.agent_id,
        "terms_id": w.terms_id,
        "plan_version_id": w.version_id,
        "revision": 1,
        "computed_at": LATER,
        "computed_by_user_id": w.admin_id,
        "base_salary": Decimal("2600000"),
        "working_days": 26,
        "worked_days": 26,
        "base_amount": Decimal("2600000"),
        "commission_amount": Decimal("0"),
        "late_commission_amount": Decimal("0"),
        "gate_visits_due": 0,
        "gate_visits_counted": 0,
        "gate_compliance_pct": None,
        "gate_multiplier": Decimal("1.000"),
        "gated_commission_amount": Decimal("0"),
        "bonus_amount": Decimal("0"),
        "penalty_amount": Decimal("0"),
        "adjustment_amount": Decimal("0"),
        "variable_amount": Decimal("0"),
        "carry_in_amount": Decimal("0"),
        "owed_in_statement_id": None,
        "gross_amount": Decimal("2600000"),
        "total_amount": Decimal("2600000"),
        "carry_out_amount": Decimal("0"),
        "owed_amount": Decimal("0"),
        "inputs": {},
    },
    AgentOrderApproval: lambda w: {
        "order_id": w.order_id,
        "outlet_id": w.outlet_id,
        "agent_user_id": w.agent_id,
        "earlier_order_ids": [DANGLING_ID],
        "status": "pending",
        "requested_at": AT,
    },
    SalesAgentProfile: lambda w: {"user_id": w.agent_id, "is_active": True, "districts": []},
}


def _closed(w):
    return {"closed_at": LATER, "closed_by_user_id": w.admin_id}


def _approved(w):
    return {"approved_at": LATER, "approved_by_user_id": w.admin_id}


def _paid(w):
    return {"paid_on": date(2026, 12, 5), "paid_recorded_at": LATER, "paid_by_user_id": w.admin_id}


def _decided(w):
    return {"decided_at": LATER, "decided_by_user_id": w.admin_id}


def _booked(w):
    return {"amount": Decimal("50000"), "period_id": w.october_id}


def _cancelled(w):
    return {"cancelled_at": LATER, "cancelled_by_user_id": w.admin_id, "cancel_reason": "Proposed twice by mistake"}


def _bonus(w):
    """A new-outlet bonus line: the one ledger line that carries an amount (v5)."""
    return {
        "kind": "new_outlet_bonus",
        "order_id": None,
        "outlet_id": w.outlet_id,
        "amount": Decimal("300000"),
        "idempotency_key": f"new_outlet_bonus:{w.outlet_id}",
    }


def _build(model, w, overrides):
    return model(**{**VALID[model](w), **overrides(w)})


ACCEPTED = [
    *[pytest.param(model, lambda w: {}, id=f"valid-{model.__tablename__}") for model in VALID],
    pytest.param(SalesPayPlanRate, lambda w: {"rate_mode": "percent", "rate_value": Decimal("12.5")},
                 id="percent-rate-may-be-fractional"),
    pytest.param(SalesPayPlanRate, lambda w: {"product_id": None}, id="default-schedule-tier"),
    pytest.param(SalesPayPlanRate, lambda w: {"from_unit": 501, "rate_value": Decimal("2000")}, id="a-later-tier"),
    pytest.param(SalesPayPeriod, lambda w: {"status": "closed", **_closed(w)}, id="closed-period"),
    pytest.param(SalesPayPeriod, lambda w: {"status": "approved", **_closed(w), **_approved(w)}, id="approved-period"),
    pytest.param(SalesPayPeriod, lambda w: {"status": "paid", **_closed(w), **_approved(w), **_paid(w)},
                 id="paid-period"),
    pytest.param(SalesPayLedgerLine, lambda w: {"kind": "commission_reversal", "cause": "not_delivered"},
                 id="reversal-not-delivered"),
    pytest.param(SalesPayLedgerLine, lambda w: {"kind": "commission_reversal", "cause": "nothing_received"},
                 id="reversal-nothing-received"),
    pytest.param(SalesPayLedgerLine, lambda w: {"kind": "commission_difference", "cause": "received_reduced"},
                 id="difference-received-reduced"),
    pytest.param(SalesPayLedgerLine, lambda w: {"kind": "commission_difference", "cause": "received_restored"},
                 id="difference-received-restored"),
    pytest.param(SalesPayLedgerLine, lambda w: {"kind": "commission_difference", "cause": "units_reduced"},
                 id="difference-units-reduced"),
    pytest.param(SalesPayLedgerLine, lambda w: {"kind": "commission_difference", "cause": "units_restored"},
                 id="difference-units-restored"),
    pytest.param(SalesPayLedgerLine, _bonus, id="new-outlet-bonus"),
    pytest.param(SalesPayLedgerLine, lambda w: {**_bonus(w), "amount": Decimal("0")}, id="new-outlet-bonus-of-nothing"),
    pytest.param(SalesPayOutletCheck, lambda w: {"status": "qualified", "ledger_line_id": w.ledger_line_id,
                                                 "window_start": AT, "window_end": LATER}, id="qualified-check"),
    pytest.param(SalesPayOutletCheck, lambda w: {"status": "not_eligible", "plan_version_id": None},
                 id="not-eligible-check"),
    pytest.param(SalesPayPenalty, lambda w: {"status": "rejected", **_decided(w), "decision_note": "No evidence"},
                 id="rejected-penalty"),
    pytest.param(SalesPayPenalty, lambda w: {"status": "confirmed", **_decided(w), **_booked(w)},
                 id="confirmed-penalty"),
    pytest.param(SalesPayPenalty, lambda w: {"status": "cancelled", **_decided(w), **_booked(w), **_cancelled(w)},
                 id="cancelled-penalty"),
    pytest.param(SalesPayPenalty, lambda w: {"origin": "direct", "status": "confirmed", **_decided(w), **_booked(w)},
                 id="direct-confirmed-penalty"),
    pytest.param(SalesPayAdjustment, lambda w: {"amount": Decimal("75000")}, id="admin-bonus-adjustment"),
    pytest.param(SalesPayAdjustment, lambda w: {"source": "carry_forward", "carried_from_period_id": w.october_id},
                 id="carry-forward"),
    pytest.param(SalesPayStatement, lambda w: {"gate_visits_due": 40, "gate_visits_counted": 35,
                                               "gate_compliance_pct": Decimal("87.5"),
                                               "gate_multiplier": Decimal("0.800")}, id="gated-statement"),
    pytest.param(SalesPayStatement, lambda w: {"worked_days": 2, "base_amount": Decimal("200000"),
                                               "penalty_amount": Decimal("300000"),
                                               "variable_amount": Decimal("-300000"),
                                               "gross_amount": Decimal("-100000"), "total_amount": Decimal("0"),
                                               "owed_amount": Decimal("100000")}, id="leaver-owes"),
    pytest.param(AgentOrderApproval, lambda w: {"status": "approved", **_decided(w)}, id="approved-hold"),
    pytest.param(AgentOrderApproval, lambda w: {"status": "rejected", **_decided(w), "reason": "Duplicate order"},
                 id="rejected-hold"),
    pytest.param(AgentOrderApproval, lambda w: {"status": "cancelled", "decided_at": LATER},
                 id="cancelled-hold-by-a-system-path"),
    pytest.param(SalesAgentProfile, lambda w: {"employment_start_date": date(2026, 10, 1),
                                               "employment_end_date": date(2026, 10, 31),
                                               "employment_set_by_user_id": w.admin_id,
                                               "employment_set_at": LATER}, id="employment-dated"),
]

REFUSED = [
    # sales_pay_plan_versions
    pytest.param(SalesPayPlanVersion, lambda w: {"bonus_window_days": 0},
                 "ck_sales_pay_plan_versions_ranges", id="version-zero-window"),
    pytest.param(SalesPayPlanVersion, lambda w: {"version_no": 0},
                 "ck_sales_pay_plan_versions_ranges", id="version-number-zero"),
    pytest.param(SalesPayPlanVersion, lambda w: {"bonus_amount": Decimal("300000.50")},
                 "ck_sales_pay_plan_versions_amounts_whole", id="version-bonus-not-whole"),
    # sales_pay_plan_rates
    pytest.param(SalesPayPlanRate, lambda w: {"rate_mode": "flat"},
                 "ck_sales_pay_plan_rates_rate_mode", id="rate-unknown-mode"),
    pytest.param(SalesPayPlanRate, lambda w: {"rate_mode": "percent", "rate_value": Decimal("101")},
                 "ck_sales_pay_plan_rates_rate_value", id="rate-percent-over-100"),
    pytest.param(SalesPayPlanRate, lambda w: {"rate_value": Decimal("0.5")},
                 "ck_sales_pay_plan_rates_rate_value", id="rate-per-unit-not-whole"),
    pytest.param(SalesPayPlanRate, lambda w: {"rate_value": Decimal("-1")},
                 "ck_sales_pay_plan_rates_rate_value", id="rate-negative"),
    pytest.param(SalesPayPlanRate, lambda w: {"from_unit": 0},
                 "ck_sales_pay_plan_rates_from_unit", id="tier-before-the-first-unit"),
    # sales_agent_pay_terms
    pytest.param(SalesAgentPayTerms, lambda w: {"base_salary": Decimal("-1")},
                 "ck_sales_agent_pay_terms_base_nonneg", id="terms-negative-base"),
    pytest.param(SalesAgentPayTerms, lambda w: {"base_salary": Decimal("2600000.50")},
                 "ck_sales_agent_pay_terms_base_salary_whole", id="terms-base-not-whole"),
    # sales_pay_periods (stamps set so that only the status rule is broken)
    pytest.param(SalesPayPeriod, lambda w: {"status": "archived", **_closed(w)},
                 "ck_sales_pay_periods_status", id="period-unknown-status"),
    pytest.param(SalesPayPeriod, lambda w: {"status": "closed"},
                 "ck_sales_pay_periods_closed_stamp", id="period-closed-without-stamp"),
    pytest.param(SalesPayPeriod, lambda w: _closed(w),
                 "ck_sales_pay_periods_closed_stamp", id="period-open-with-closed-stamp"),
    pytest.param(SalesPayPeriod, lambda w: {"status": "closed", "closed_at": LATER},
                 "ck_sales_pay_periods_closed_stamp", id="period-closed-without-actor"),
    pytest.param(SalesPayPeriod, lambda w: {"status": "approved", **_closed(w)},
                 "ck_sales_pay_periods_approved_stamp", id="period-approved-without-stamp"),
    pytest.param(SalesPayPeriod, lambda w: {"status": "approved", **_closed(w), "approved_at": LATER},
                 "ck_sales_pay_periods_approved_stamp", id="period-approved-without-actor"),
    pytest.param(SalesPayPeriod, lambda w: {"status": "paid", **_closed(w), **_approved(w)},
                 "ck_sales_pay_periods_paid_stamp", id="period-paid-without-stamp"),
    pytest.param(SalesPayPeriod, lambda w: {"status": "paid", **_closed(w), **_approved(w),
                                            "paid_on": date(2026, 12, 5), "paid_recorded_at": LATER},
                 "ck_sales_pay_periods_paid_stamp", id="period-paid-without-actor"),
    # sales_pay_ledger_lines. An unknown kind also breaks the per-kind cause and amount rules;
    # the kind CHECK is declared first, so SQLite names it (Postgres picks by name, which is
    # why the Postgres proofs never probe an unknown kind).
    pytest.param(SalesPayLedgerLine, lambda w: {"kind": "bonus"},
                 "ck_sales_pay_ledger_lines_kind", id="ledger-unknown-kind"),
    pytest.param(SalesPayLedgerLine, lambda w: {"cause": "not_delivered"},
                 "ck_sales_pay_ledger_lines_cause", id="ledger-credit-with-cause"),
    # `NULL IN (...)` is NULL and a CHECK passes on NULL, so the cause rule spells out
    # `cause IS NOT NULL` for the two kinds that need one (R1-1). `amount` is NULL here, as on
    # every commission line, so only the cause rule can refuse these.
    pytest.param(SalesPayLedgerLine, lambda w: {"kind": "commission_reversal"},
                 "ck_sales_pay_ledger_lines_cause", id="ledger-reversal-without-cause"),
    pytest.param(SalesPayLedgerLine, lambda w: {"kind": "commission_difference"},
                 "ck_sales_pay_ledger_lines_cause", id="ledger-difference-without-cause"),
    pytest.param(SalesPayLedgerLine, lambda w: {"kind": "commission_reversal", "cause": "units_reduced"},
                 "ck_sales_pay_ledger_lines_cause", id="ledger-reversal-with-difference-cause"),
    pytest.param(SalesPayLedgerLine, lambda w: {"kind": "commission_difference", "cause": "plan_changed"},
                 "ck_sales_pay_ledger_lines_cause", id="ledger-retired-plan-changed"),
    pytest.param(SalesPayLedgerLine, lambda w: {"outlet_id": w.outlet_id},
                 "ck_sales_pay_ledger_lines_subject", id="ledger-credit-with-outlet"),
    pytest.param(SalesPayLedgerLine, lambda w: {"kind": "new_outlet_bonus"},
                 "ck_sales_pay_ledger_lines_subject", id="ledger-bonus-on-an-order"),
    # v5: a commission line records a level and never money; a bonus is the one line with an amount.
    pytest.param(SalesPayLedgerLine, lambda w: {"amount": Decimal("9000")},
                 "ck_sales_pay_ledger_lines_amount_kind", id="ledger-credit-with-an-amount"),
    pytest.param(SalesPayLedgerLine, lambda w: {"kind": "commission_difference", "cause": "units_reduced",
                                                "amount": Decimal("-1500")},
                 "ck_sales_pay_ledger_lines_amount_kind", id="ledger-difference-with-an-amount"),
    pytest.param(SalesPayLedgerLine, lambda w: {"kind": "commission_reversal", "cause": "nothing_received",
                                                "amount": Decimal("0")},
                 "ck_sales_pay_ledger_lines_amount_kind", id="ledger-reversal-with-an-amount"),
    pytest.param(SalesPayLedgerLine, lambda w: {**_bonus(w), "amount": None},
                 "ck_sales_pay_ledger_lines_amount_kind", id="ledger-bonus-without-an-amount"),
    pytest.param(SalesPayLedgerLine, lambda w: {**_bonus(w), "amount": Decimal("-1")},
                 "ck_sales_pay_ledger_lines_amount_kind", id="ledger-negative-bonus"),
    pytest.param(SalesPayLedgerLine, lambda w: {**_bonus(w), "amount": Decimal("300000.50")},
                 "ck_sales_pay_ledger_lines_amount_whole", id="ledger-amount-not-whole"),
    # sales_pay_outlet_checks
    pytest.param(SalesPayOutletCheck, lambda w: {"status": "paused"},
                 "ck_sales_pay_outlet_checks_status", id="check-unknown-status"),
    pytest.param(SalesPayOutletCheck, lambda w: {"status": "qualified"},
                 "ck_sales_pay_outlet_checks_qualified", id="check-qualified-without-line"),
    pytest.param(SalesPayOutletCheck, lambda w: {"ledger_line_id": w.ledger_line_id},
                 "ck_sales_pay_outlet_checks_qualified", id="check-tracking-with-line"),
    pytest.param(SalesPayOutletCheck, lambda w: {"window_end": LATER},
                 "ck_sales_pay_outlet_checks_window_pair", id="check-window-end-without-start"),
    pytest.param(SalesPayOutletCheck, lambda w: {"window_start": AT, "window_end": AT},
                 "ck_sales_pay_outlet_checks_window_pair", id="check-empty-window"),
    # sales_pay_penalty_types
    pytest.param(SalesPayPenaltyType, lambda w: {"default_amount": Decimal("0")},
                 "ck_sales_pay_penalty_types_default_amount_positive", id="type-zero-amount"),
    pytest.param(SalesPayPenaltyType, lambda w: {"default_amount": Decimal("50000.50")},
                 "ck_sales_pay_penalty_types_default_amount_whole", id="type-amount-not-whole"),
    # sales_pay_penalties
    pytest.param(SalesPayPenalty, lambda w: {"status": "escalated", **_decided(w)},
                 "ck_sales_pay_penalties_status", id="penalty-unknown-status"),
    pytest.param(SalesPayPenalty, lambda w: {"origin": "manual"},
                 "ck_sales_pay_penalties_origin", id="penalty-unknown-origin"),
    pytest.param(SalesPayPenalty, lambda w: _booked(w),
                 "ck_sales_pay_penalties_booked", id="penalty-proposed-but-booked"),
    pytest.param(SalesPayPenalty, lambda w: {"status": "confirmed", **_decided(w)},
                 "ck_sales_pay_penalties_booked", id="penalty-confirmed-but-not-booked"),
    pytest.param(SalesPayPenalty, lambda w: _decided(w),
                 "ck_sales_pay_penalties_decided", id="penalty-proposed-but-decided"),
    pytest.param(SalesPayPenalty, lambda w: {"status": "rejected"},
                 "ck_sales_pay_penalties_decided", id="penalty-rejected-undecided"),
    pytest.param(SalesPayPenalty, lambda w: {"status": "cancelled", **_decided(w), **_booked(w)},
                 "ck_sales_pay_penalties_cancelled", id="penalty-cancelled-without-stamp"),
    pytest.param(SalesPayPenalty, lambda w: {"status": "cancelled", **_decided(w), **_booked(w), **_cancelled(w),
                                             "cancel_reason": None},
                 "ck_sales_pay_penalties_cancelled", id="penalty-cancelled-without-reason"),
    pytest.param(SalesPayPenalty, lambda w: {"status": "confirmed", **_decided(w), **_booked(w),
                                             "amount": Decimal("-50000")},
                 "ck_sales_pay_penalties_amount_positive", id="penalty-negative-amount"),
    pytest.param(SalesPayPenalty, lambda w: {"status": "confirmed", **_decided(w), **_booked(w),
                                             "amount": Decimal("50000.50")},
                 "ck_sales_pay_penalties_amount_whole", id="penalty-amount-not-whole"),
    pytest.param(SalesPayPenalty, lambda w: {"origin": "direct"},
                 "ck_sales_pay_penalties_direct_confirmed", id="penalty-direct-but-proposed"),
    # sales_pay_adjustments
    pytest.param(SalesPayAdjustment, lambda w: {"amount": Decimal("0")},
                 "ck_sales_pay_adjustments_amount_nonzero", id="adjustment-zero"),
    pytest.param(SalesPayAdjustment, lambda w: {"amount": Decimal("-50000.50")},
                 "ck_sales_pay_adjustments_amount_whole", id="adjustment-not-whole"),
    pytest.param(SalesPayAdjustment, lambda w: {"source": "bonus"},
                 "ck_sales_pay_adjustments_source", id="adjustment-unknown-source"),
    pytest.param(SalesPayAdjustment, lambda w: {"source": "carry_forward"},
                 "ck_sales_pay_adjustments_carry", id="carry-without-source-month"),
    pytest.param(SalesPayAdjustment, lambda w: {"carried_from_period_id": w.october_id},
                 "ck_sales_pay_adjustments_carry", id="admin-row-with-source-month"),
    pytest.param(SalesPayAdjustment, lambda w: {"source": "carry_forward", "carried_from_period_id": w.october_id,
                                                "amount": Decimal("50000")},
                 "ck_sales_pay_adjustments_carry_sign", id="carry-that-is-not-a-shortfall"),
    # sales_pay_statements
    pytest.param(SalesPayStatement, lambda w: {"base_amount": Decimal("2600000.50"),
                                               "gross_amount": Decimal("2600000.50"),
                                               "total_amount": Decimal("2600000.50")},
                 "ck_sales_pay_statements_amounts_whole", id="statement-not-whole"),
    pytest.param(SalesPayStatement, lambda w: {"total_amount": Decimal("-1")},
                 "ck_sales_pay_statements_nonneg", id="statement-negative-total"),
    pytest.param(SalesPayStatement, lambda w: {"carry_in_amount": Decimal("100000"),
                                               "gross_amount": Decimal("2700000")},
                 "ck_sales_pay_statements_nonneg", id="statement-positive-carry-in"),
    pytest.param(SalesPayStatement, lambda w: {"carry_out_amount": Decimal("1")},
                 "ck_sales_pay_statements_nonneg", id="statement-positive-carry-out"),
    pytest.param(SalesPayStatement, lambda w: {"owed_amount": Decimal("-1")},
                 "ck_sales_pay_statements_nonneg", id="statement-negative-owed"),
    pytest.param(SalesPayStatement, lambda w: {"gross_amount": Decimal("2600001")},
                 "ck_sales_pay_statements_total", id="statement-gross-not-the-sum"),
    pytest.param(SalesPayStatement, lambda w: {"owed_in_statement_id": DANGLING_ID},
                 "ck_sales_pay_statements_owed_in", id="statement-nets-without-carry-in"),
    pytest.param(SalesPayStatement, lambda w: {"worked_days": 27},
                 "ck_sales_pay_statements_days", id="statement-worked-more-than-working"),
    pytest.param(SalesPayStatement, lambda w: {"gate_multiplier": Decimal("1.500")},
                 "ck_sales_pay_statements_gate", id="statement-multiplier-over-1"),
    pytest.param(SalesPayStatement, lambda w: {"gate_compliance_pct": Decimal("100.1")},
                 "ck_sales_pay_statements_gate", id="statement-compliance-over-100"),
    pytest.param(SalesPayStatement, lambda w: {"revision": 0},
                 "ck_sales_pay_statements_gate", id="statement-revision-zero"),
    # agent_order_approvals (the five §10.5 cells, plus the unknown status and a stray reason)
    pytest.param(AgentOrderApproval, lambda w: {"status": "expired", **_decided(w)},
                 "ck_agent_order_approvals_status", id="hold-unknown-status"),
    pytest.param(AgentOrderApproval, lambda w: {"status": "approved", "decided_at": LATER},
                 "ck_agent_order_approvals_decider", id="hold-approved-without-decider"),
    pytest.param(AgentOrderApproval, lambda w: {"status": "rejected", **_decided(w)},
                 "ck_agent_order_approvals_reason", id="hold-rejected-without-reason"),
    pytest.param(AgentOrderApproval, lambda w: {"decided_at": LATER},
                 "ck_agent_order_approvals_decided", id="hold-pending-with-decided-at"),
    pytest.param(AgentOrderApproval, lambda w: {"decided_by_user_id": w.admin_id},
                 "ck_agent_order_approvals_decider", id="hold-pending-with-decider"),
    pytest.param(AgentOrderApproval, lambda w: {"status": "approved", **_decided(w), "reason": "Fine"},
                 "ck_agent_order_approvals_reason", id="hold-approved-with-reason"),
    # sales_agent_profiles
    pytest.param(SalesAgentProfile, lambda w: {"employment_start_date": date(2026, 10, 1),
                                               "employment_end_date": date(2026, 9, 30)},
                 "ck_sales_agent_profiles_employment_dates_order", id="employment-ends-before-it-starts"),
    pytest.param(SalesAgentProfile, lambda w: {"employment_set_at": LATER},
                 "ck_sales_agent_profiles_employment_set_pair", id="employment-set-without-actor"),
    pytest.param(SalesAgentProfile, lambda w: {"employment_set_by_user_id": w.admin_id},
                 "ck_sales_agent_profiles_employment_set_pair", id="employment-actor-without-time"),
]

# (model, first row or None when the world already holds it, the duplicate, the columns SQLite
# names, the constraint the duplicate hits)
UNIQUES = [
    pytest.param(SalesPayPlan, None, lambda w: {"name": "Standard"},
                 "sales_pay_plans.name", "uq_sales_pay_plans_name", id="plan-name"),
    pytest.param(SalesPayPlanVersion, None, lambda w: {"version_no": 1},
                 "sales_pay_plan_versions.plan_id, sales_pay_plan_versions.version_no",
                 "uq_sales_pay_plan_versions_plan_version", id="version-number"),
    pytest.param(SalesPayPlanRate, lambda w: {}, lambda w: {"rate_value": Decimal("2000")},
                 "sales_pay_plan_rates.plan_version_id, sales_pay_plan_rates.product_id, "
                 "sales_pay_plan_rates.from_unit",
                 "uq_sales_pay_plan_rates_version_product_from", id="one-tier-per-product-and-first-unit"),
    pytest.param(SalesPayPeriod, None, lambda w: {"month_start": OCT},
                 "sales_pay_periods.month_start", "uq_sales_pay_periods_month_start", id="period-month"),
    pytest.param(SalesAgentUnpaidDay, lambda w: {}, lambda w: {"note": "Entered twice"},
                 "sales_agent_unpaid_days.agent_user_id, sales_agent_unpaid_days.unpaid_date",
                 "uq_sales_agent_unpaid_days_agent_date", id="unpaid-day"),
    pytest.param(SalesPayLedgerLine, None, lambda w: {"idempotency_key": f"commission:{w.order_id}:1"},
                 "sales_pay_ledger_lines.idempotency_key", "uq_sales_pay_ledger_lines_idempotency_key",
                 id="ledger-idempotency-key"),
    pytest.param(SalesPayOutletCheck, lambda w: {}, lambda w: {"status": "expired"},
                 "sales_pay_outlet_checks.outlet_id", "uq_sales_pay_outlet_checks_outlet_id", id="one-check-per-outlet"),
    pytest.param(SalesPayAdjustment,
                 lambda w: {"source": "carry_forward", "carried_from_period_id": w.october_id},
                 lambda w: {"source": "carry_forward", "carried_from_period_id": w.october_id,
                            "amount": Decimal("-1000")},
                 "sales_pay_adjustments.carried_from_period_id, sales_pay_adjustments.agent_user_id",
                 "uq_sales_pay_adjustments_carry_from_agent", id="one-carry-per-agent-and-source-month"),
    pytest.param(SalesPayStatement, lambda w: {}, lambda w: {"revision": 2},
                 "sales_pay_statements.period_id, sales_pay_statements.agent_user_id",
                 "uq_sales_pay_statements_period_agent", id="one-statement-per-agent-and-month"),
    pytest.param(SalesPayStatement,
                 lambda w: {"period_id": w.november_id, "carry_in_amount": Decimal("-100000"),
                            "gross_amount": Decimal("2500000"), "total_amount": Decimal("2500000"),
                            "owed_in_statement_id": DANGLING_ID},
                 lambda w: {"carry_in_amount": Decimal("-100000"), "gross_amount": Decimal("2500000"),
                            "total_amount": Decimal("2500000"), "owed_in_statement_id": DANGLING_ID},
                 "sales_pay_statements.owed_in_statement_id", "uq_sales_pay_statements_owed_in_statement_id",
                 id="one-netting-per-owed-balance"),
    pytest.param(AgentOrderApproval, lambda w: {}, lambda w: {"earlier_order_ids": [DANGLING_ID, DANGLING_ID + 1]},
                 "agent_order_approvals.order_id", "uq_agent_order_approvals_order_id", id="one-hold-per-order"),
]


@pytest.mark.unit
@pytest.mark.parametrize("model, overrides", ACCEPTED)
def test_the_schema_accepts_each_legal_row(db, world, model, overrides):
    row = _build(model, world, overrides)
    db.session.add(row)
    db.session.flush()

    assert row.id is not None


@pytest.mark.unit
@pytest.mark.parametrize("model, overrides, constraint", REFUSED)
def test_each_check_refuses_its_bad_row(db, world, model, overrides, constraint):
    db.session.add(_build(model, world, overrides))

    with pytest.raises(IntegrityError) as exc_info:
        db.session.flush()

    assert str(exc_info.value.orig) == f"CHECK constraint failed: {constraint}"
    db.session.rollback()


@pytest.mark.unit
@pytest.mark.parametrize("model, first, duplicate, columns, constraint", UNIQUES)
def test_each_unique_refuses_its_duplicate(db, world, model, first, duplicate, columns, constraint):
    if first is not None:
        db.session.add(_build(model, world, first))
        db.session.flush()
    db.session.add(_build(model, world, duplicate))

    with pytest.raises(IntegrityError) as exc_info:
        db.session.flush()

    assert str(exc_info.value.orig) == f"UNIQUE constraint failed: {columns}"
    db.session.rollback()


@pytest.mark.unit
def test_every_declared_check_has_a_refusal_case():
    """A CHECK added without a probe fails here. The first-of-month CHECKs are Postgres-only."""
    declared = {
        constraint.name
        for model in (*PAY_MODELS, SalesAgentProfile)
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint) and not constraint.name.endswith("_first_of_month")
    }

    assert declared == {case.values[2] for case in REFUSED}


@pytest.mark.unit
def test_every_declared_unique_has_a_duplicate_case():
    declared = {
        constraint.name
        for model in PAY_MODELS
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert declared == {case.values[4] for case in UNIQUES}


@pytest.mark.unit
def test_a_version_keeps_no_rate_of_its_own_and_a_tier_may_belong_to_no_product():
    """V5-B2: the default rate's one home is the default schedule, i.e. tier rows whose `product_id`
    is NULL. Nothing on the version row can disagree with it."""
    assert {"default_rate_mode", "default_rate_value"}.isdisjoint(SalesPayPlanVersion.__table__.columns.keys())
    assert SalesPayPlanRate.__table__.columns["product_id"].nullable is True
    assert SalesPayPlanRate.__table__.columns["from_unit"].nullable is False


@pytest.mark.unit
def test_the_default_schedule_holds_one_tier_per_first_unit(db, world):
    """§3.2: NULLs are distinct in `uq_sales_pay_plan_rates_version_product_from`, so a partial unique
    index keeps the default schedule to one tier per `from_unit`. It is an `Index`, which the
    completeness pin above does not list, and SQLite enforces it too (`sqlite_where`). Two products
    may each start a tier at the same unit."""
    db.session.add_all(
        [
            _build(SalesPayPlanRate, world, lambda w: {"product_id": None, "rate_mode": "percent",
                                                       "rate_value": Decimal("3")}),
            _build(SalesPayPlanRate, world, lambda w: {}),
            _build(SalesPayPlanRate, world, lambda w: {"product_id": w.product_id + 1}),
        ]
    )
    db.session.flush()
    db.session.add(_build(SalesPayPlanRate, world, lambda w: {"product_id": None, "rate_value": Decimal("2000")}))

    with pytest.raises(IntegrityError) as exc_info:
        db.session.flush()

    assert str(exc_info.value.orig) == (
        "UNIQUE constraint failed: sales_pay_plan_rates.plan_version_id, sales_pay_plan_rates.from_unit"
    )
    db.session.rollback()


@pytest.mark.unit
def test_every_constraint_and_index_is_named_within_the_postgres_limit():
    """§3.1: every FK, UQ, CK and IX is named, and Postgres truncates past 63 characters.

    A truncated name no longer matches the name `downgrade()` drops by.
    """
    for model in PAY_MODELS:
        table = model.__table__
        named = [c for c in table.constraints if not isinstance(c, PrimaryKeyConstraint)] + list(table.indexes)
        for item in named:
            assert item.name, f"{table.name}: unnamed {type(item).__name__} on {[c.name for c in item.columns]}"
            assert len(item.name) <= 63, f"{item.name} is {len(item.name)} characters"


@pytest.mark.unit
def test_an_owed_balance_is_netted_by_at_most_one_statement(db, world):
    """T-OWED-3 (d): the duplicate backstop for netting (I-28).

    October's leaver statement owes 100,000 (gross 200,000 − 300,000 penalties = −100,000).
    November nets it: carry-in −100,000, so gross = 2,600,000 − 100,000 = 2,500,000. A second
    statement netting the same balance is refused by the unique, and a netting with no carry-in
    is refused by the CHECK. Statements that net nothing (owed_in NULL) are unaffected, because
    NULLs are distinct in a unique.
    """
    december = SalesPayPeriod(month_start=DEC, status="open", is_shadow=False, holidays=[])
    other_agent = User(phone="+998900000302", password_hash="not-a-real-hash", first_name="Bobur")
    db.session.add_all([december, other_agent])
    db.session.flush()

    source = _build(SalesPayStatement, world, lambda w: {
        "worked_days": 2,
        "base_amount": Decimal("200000"),
        "penalty_amount": Decimal("300000"),
        "variable_amount": Decimal("-300000"),
        "gross_amount": Decimal("-100000"),
        "total_amount": Decimal("0"),
        "owed_amount": Decimal("100000"),
    })
    db.session.add(source)
    db.session.flush()
    netting = {
        "carry_in_amount": Decimal("-100000"),
        "gross_amount": Decimal("2500000"),
        "total_amount": Decimal("2500000"),
        "owed_in_statement_id": source.id,
    }
    november = _build(SalesPayStatement, world, lambda w: {"period_id": w.november_id, **netting})
    db.session.add(november)
    db.session.commit()
    december_id, november_statement_id, other_agent_id = december.id, november.id, other_agent.id

    db.session.add(_build(SalesPayStatement, world, lambda w: {"period_id": december_id, **netting}))
    with pytest.raises(IntegrityError) as exc_info:
        db.session.flush()
    assert str(exc_info.value.orig) == "UNIQUE constraint failed: sales_pay_statements.owed_in_statement_id"
    db.session.rollback()

    # Pointed at a statement nobody has netted yet, so only the CHECK can refuse it.
    db.session.add(_build(SalesPayStatement, world, lambda w: {
        "period_id": december_id,
        "owed_in_statement_id": november_statement_id,
    }))
    with pytest.raises(IntegrityError) as exc_info:
        db.session.flush()
    assert str(exc_info.value.orig) == "CHECK constraint failed: ck_sales_pay_statements_owed_in"
    db.session.rollback()

    db.session.add_all([
        _build(SalesPayStatement, world, lambda w: {"period_id": december_id}),
        _build(SalesPayStatement, world, lambda w: {"agent_user_id": other_agent_id}),
        _build(SalesPayStatement, world, lambda w: {"agent_user_id": other_agent_id, "period_id": w.november_id}),
    ])
    db.session.commit()
    assert SalesPayStatement.query.filter(SalesPayStatement.owed_in_statement_id.is_(None)).count() == 4


@pytest.mark.unit
def test_two_admin_adjustments_for_one_agent_and_month_are_both_kept(db, world):
    """Admin rows carry no source month, and NULLs are distinct, so the carry unique never
    reaches them: a bonus and a deduction in one month are two rows."""
    db.session.add_all([
        _build(SalesPayAdjustment, world, lambda w: {"amount": Decimal("75000"), "reason": "Top outlet of the week"}),
        _build(SalesPayAdjustment, world, lambda w: {"amount": Decimal("-20000"), "reason": "Lost price tags"}),
    ])
    db.session.commit()

    assert SalesPayAdjustment.query.filter_by(agent_user_id=world.agent_id, source="admin").count() == 2
