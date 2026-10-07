"""The sales-agent pay vocabulary: one block, pinned, and read by the models' CHECKs.

Spec docs/superpowers/specs/2026-09-28-sales-agent-compensation-design.md §2.3 names every
tuple in the `# ---- SALES PAY ----` block of `shared/staff_constants.py`. The backend models,
the staff bot, `staff_bot/i18n.py` and the staff seed import them, and migration b7e3c1a95d42
keeps frozen literal copies. So each name is pinned here exactly as the spec spells it: a
reordered or renamed member is a contract change, never a refactor. Each model CHECK over a
tuple is pinned to `_quoted(tuple)`, because a value added to the tuple alone would be offered
by a service and then refused by the database.

The migrated schema's frozen copies are pinned against the same tuples on Postgres, in
tests/integration/test_sales_pay_pg_constraints.py.

The model imports sit inside the tests that need them, so the tuple pins can pass before
the models exist.
"""

import importlib
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint

from business_app.models.sales import _quoted
from business_app.utils.constants import ORDER_SOURCE_PREFIXES, ORDER_SOURCE_SALES_AGENT
from business_app.utils.state_validators import STAFF_ORDER_SOURCES
from shared import staff_constants

REPO_ROOT = Path(__file__).resolve().parents[2]
VISIT_SERVICE = REPO_ROOT / "business_app" / "services" / "sales" / "visit_service.py"
STATE_VALIDATORS = REPO_ROOT / "business_app" / "utils" / "state_validators.py"

# Spec §2.3, transcribed. DATA, not a re-derivation: the commission kinds are spelled out
# rather than sliced, so a reorder of the ledger kinds shows up here.
PINNED = {
    "SALES_PAY_PERIOD_STATUSES": ("open", "closed", "approved", "paid"),
    "SALES_PAY_PERIOD_ACTIONS": ("close", "recalculate", "approve", "mark_paid"),
    "SALES_PAY_RATE_MODES": ("per_unit", "percent"),
    "SALES_PAY_LEDGER_KINDS": (
        "commission_credit",
        "commission_reversal",
        "commission_difference",
        "new_outlet_bonus",
    ),
    "SALES_PAY_COMMISSION_KINDS": ("commission_credit", "commission_reversal", "commission_difference"),
    "SALES_PAY_REVERSAL_CAUSES": ("not_delivered", "nothing_received"),
    "SALES_PAY_DIFFERENCE_CAUSES": ("received_reduced", "received_restored", "units_reduced", "units_restored"),
    "SALES_PAY_PENALTY_STATUSES": ("proposed", "confirmed", "rejected", "cancelled"),
    "SALES_PAY_PENALTY_ORIGINS": ("proposal", "direct"),
    "SALES_PAY_ADJUSTMENT_SOURCES": ("admin", "carry_forward"),
    "SALES_PAY_CARRY_IN_SOURCES": ("carry_forward", "owed"),
    "SALES_PAY_OUTLET_CHECK_STATUSES": ("tracking", "qualified", "prior_customer", "not_eligible", "expired"),
    "SALES_PAY_BONUS_RULES": ("orders_with_total", "orders_any"),
    "SALES_PAY_GATE_RULES": ("band", "below_min_due"),
    "SALES_PAY_DAY_STATUSES": ("worked", "non_working", "holiday", "unpaid", "not_employed"),
    "SALES_PAY_PIPELINE_WAITING": ("approval", "delivery", "payment"),
    "SALES_PAY_SELF_DECISION_INPUTS": (
        "terms",
        "employment",
        "unpaid_day",
        "adjustment",
        "penalty",
        "order_approval",
    ),
    "SALES_PAY_SELF_DECISION_ACTIONS": (
        "added",
        "set",
        "created",
        "proposed",
        "confirmed",
        "cancelled",
        "approved",
    ),
    "SALES_PAY_REASON_MIN_LENGTH": 5,
    "SALES_EVENT_PAY_PENALTY_CONFIRMED": "pay_penalty_confirmed",
    "SALES_EVENT_PAY_STATEMENT_APPROVED": "pay_statement_approved",
    "SALES_AGENT_ORDER_STATES": (
        "auto_confirmed",
        "pending_confirmation",
        "confirmed",
        "awaiting_staff_approval",
    ),
    "SALES_EVENT_AGENT_ORDER_APPROVED": "agent_order_approved",
    "SALES_EVENT_AGENT_ORDER_REJECTED": "agent_order_rejected",
}

# (model module, class, CHECK name, column, (tuple module, tuple name)): every CHECK that is a
# plain `<column> IN (<tuple>)`.
VOCABULARY_CHECKS = [
    ("business_app.models.sales_pay", "SalesPayPlanRate", "ck_sales_pay_plan_rates_rate_mode",
     "rate_mode", ("shared.staff_constants", "SALES_PAY_RATE_MODES")),
    ("business_app.models.sales_pay", "SalesPayPeriod", "ck_sales_pay_periods_status",
     "status", ("shared.staff_constants", "SALES_PAY_PERIOD_STATUSES")),
    ("business_app.models.sales_pay", "SalesPayLedgerLine", "ck_sales_pay_ledger_lines_kind",
     "kind", ("shared.staff_constants", "SALES_PAY_LEDGER_KINDS")),
    ("business_app.models.sales_pay", "SalesPayOutletCheck", "ck_sales_pay_outlet_checks_status",
     "status", ("shared.staff_constants", "SALES_PAY_OUTLET_CHECK_STATUSES")),
    ("business_app.models.sales_pay", "SalesPayPenalty", "ck_sales_pay_penalties_status",
     "status", ("shared.staff_constants", "SALES_PAY_PENALTY_STATUSES")),
    ("business_app.models.sales_pay", "SalesPayPenalty", "ck_sales_pay_penalties_origin",
     "origin", ("shared.staff_constants", "SALES_PAY_PENALTY_ORIGINS")),
    ("business_app.models.sales_pay", "SalesPayAdjustment", "ck_sales_pay_adjustments_source",
     "source", ("shared.staff_constants", "SALES_PAY_ADJUSTMENT_SOURCES")),
    ("business_app.models.sales_visits", "AgentOrderApproval", "ck_agent_order_approvals_status",
     "status", ("business_app.models.sales_visits", "AGENT_ORDER_APPROVAL_STATUSES")),
]


def _check_sql(model, name):
    """The SQL text of the one CHECK named ``name`` on ``model``'s table."""
    matches = [
        constraint
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name == name
    ]
    assert len(matches) == 1, f"{model.__tablename__} declares {len(matches)} CHECKs named {name}"
    return str(matches[0].sqltext)


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(PINNED))
def test_every_pay_name_is_spelled_as_the_spec_names_it(name):
    assert hasattr(staff_constants, name), f"shared/staff_constants.py is missing {name}"
    value = getattr(staff_constants, name)
    assert value == PINNED[name]
    assert type(value) is type(PINNED[name])


@pytest.mark.unit
def test_the_sales_agent_order_source_has_one_spelling():
    """Review SSOT M4: the writer, the staff-source guard and pay attribution share one constant.

    `pay_rules.agent_placed_clause` (Task 3) reads it too. A literal left at the writer would
    let the two drift apart silently: orders would be written under one spelling and
    attributed under another, so every agent's commission would read zero.
    """
    assert ORDER_SOURCE_SALES_AGENT == "sales_agent"
    assert ORDER_SOURCE_PREFIXES[ORDER_SOURCE_SALES_AGENT] == "SA"
    assert ORDER_SOURCE_SALES_AGENT in STAFF_ORDER_SOURCES

    visit_service = VISIT_SERVICE.read_text(encoding="utf-8")
    assert '"order_source": ORDER_SOURCE_SALES_AGENT' in visit_service
    assert '"order_source": "sales_agent"' not in visit_service
    assert '"sales_agent"' not in STATE_VALIDATORS.read_text(encoding="utf-8")


@pytest.mark.unit
@pytest.mark.parametrize(
    "module_path, class_name, constraint, column, tuple_ref",
    VOCABULARY_CHECKS,
    ids=[row[2] for row in VOCABULARY_CHECKS],
)
def test_each_vocabulary_check_lists_exactly_its_tuple(module_path, class_name, constraint, column, tuple_ref):
    model = getattr(importlib.import_module(module_path), class_name)
    values = getattr(importlib.import_module(tuple_ref[0]), tuple_ref[1])

    assert _check_sql(model, constraint) == f"{column} IN ({_quoted(values)})"


@pytest.mark.unit
def test_the_ledger_cause_check_lists_both_cause_tuples():
    """The cause CHECK pairs each kind with its own cause tuple, so it holds two IN lists."""
    from business_app.models.sales_pay import SalesPayLedgerLine

    sql = _check_sql(SalesPayLedgerLine, "ck_sales_pay_ledger_lines_cause")

    assert f"cause IN ({_quoted(staff_constants.SALES_PAY_REVERSAL_CAUSES)})" in sql
    assert f"cause IN ({_quoted(staff_constants.SALES_PAY_DIFFERENCE_CAUSES)})" in sql


MIGRATION_V5 = REPO_ROOT / "business_app" / "migrations" / "versions" / "20261005_1200_sales_pay_rate_tiers.py"


@pytest.mark.unit
def test_the_v5_migration_writes_the_models_ledger_checks_from_frozen_copies():
    """§3.5b, R1-1: c5d8e2f1a7b3 keeps frozen copies of the cause tuples (a migration never imports a
    live one). Its v5 copy equals the live tuple, `_cause_rule` builds exactly the model's cause
    CHECK (`cause IS NOT NULL` in both branches), and `V5_AMOUNT_KIND` is the model's amount CHECK,
    so the migrated schema and `create_all` hold one text each. The v4 copy still names
    `plan_changed`: the downgrade restores v4's CHECK."""
    import importlib.util

    from business_app.models.sales_pay import SalesPayLedgerLine

    spec = importlib.util.spec_from_file_location("sales_pay_rate_tiers", MIGRATION_V5)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.V5_DIFFERENCE_CAUSES == staff_constants.SALES_PAY_DIFFERENCE_CAUSES
    assert migration.REVERSAL_CAUSES == staff_constants.SALES_PAY_REVERSAL_CAUSES
    assert migration.V4_DIFFERENCE_CAUSES == ("received_reduced", "received_restored", "plan_changed")
    assert migration._cause_rule(migration.V5_DIFFERENCE_CAUSES) == _check_sql(
        SalesPayLedgerLine, "ck_sales_pay_ledger_lines_cause"
    )
    assert migration.V5_AMOUNT_KIND == _check_sql(SalesPayLedgerLine, "ck_sales_pay_ledger_lines_amount_kind")


@pytest.mark.unit
def test_the_approval_hold_statuses_are_pinned():
    """The hold's row vocabulary lives beside its sibling `CONFIRMATION_STATUSES` (§2.3)."""
    from business_app.models.sales_visits import AGENT_ORDER_APPROVAL_STATUSES

    assert AGENT_ORDER_APPROVAL_STATUSES == ("pending", "approved", "rejected", "cancelled")
