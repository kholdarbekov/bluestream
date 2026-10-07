"""Postgres-only guarantees of migrations b7e3c1a95d42 (sales-agent pay) and c5d8e2f1a7b3 (v5 tiers),
which SQLite cannot see.

tests/unit/test_sales_pay_models.py proves the MODEL's constraints on SQLite. This file proves
the MIGRATION:
- it carries exactly the model's constraint and index names, columns and nullability;
- its first-of-month CHECKs exist (they are Postgres-only);
- its frozen vocabulary copies list exactly the live tuples;
- the two §10.5 money rules hold by name.

Each test runs the real migrations on a throwaway database (`pg_db`).
"""

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import PrimaryKeyConstraint, text

from business_app.models.sales import SalesAgentProfile
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
from business_app.models.sales_visits import AGENT_ORDER_APPROVAL_STATUSES, AgentOrderApproval
from business_app.models.user import User
from business_app.utils.constants import ORDER_SOURCE_SALES_AGENT
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
from tests.integration.test_sales_agent_pg_constraints import (
    _agent_and_outlet,
    _constraint_def,
    _index_def,
    _order,
    _quoted_literals,
    _rollback_on_named_integrity,
)
from tests.integration.sales_pay_builders import make_product
from tests.unit.test_sales_pay_models import _version_fields

pytestmark = pytest.mark.integration

OCT = date(2026, 10, 1)
NOV = date(2026, 11, 1)
MID_OCTOBER = date(2026, 10, 5)
AT = datetime(2026, 10, 4, 9, 12, tzinfo=UTC)
LATER = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

NEW_TABLE_MODELS = (
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

PAY_VOCABULARY_CHECKS = {
    "ck_sales_pay_plan_rates_rate_mode": ("sales_pay_plan_rates", SALES_PAY_RATE_MODES),
    "ck_sales_pay_periods_status": ("sales_pay_periods", SALES_PAY_PERIOD_STATUSES),
    "ck_sales_pay_ledger_lines_kind": ("sales_pay_ledger_lines", SALES_PAY_LEDGER_KINDS),
    "ck_sales_pay_outlet_checks_status": ("sales_pay_outlet_checks", SALES_PAY_OUTLET_CHECK_STATUSES),
    "ck_sales_pay_penalties_status": ("sales_pay_penalties", SALES_PAY_PENALTY_STATUSES),
    "ck_sales_pay_penalties_origin": ("sales_pay_penalties", SALES_PAY_PENALTY_ORIGINS),
    "ck_sales_pay_adjustments_source": ("sales_pay_adjustments", SALES_PAY_ADJUSTMENT_SOURCES),
    "ck_agent_order_approvals_status": ("agent_order_approvals", AGENT_ORDER_APPROVAL_STATUSES),
}


def _pay_world(pg_db):
    """An agent, their outlet and order, a plan with its October version, and October open."""
    agent, outlet = _agent_and_outlet(pg_db, phone="+998900000401", outlet_name="Pay Shop")
    customer = User(phone="+998900000402", password_hash="not-a-real-hash", first_name="Store")
    pg_db.session.add(customer)
    pg_db.session.commit()
    order = _order(pg_db, user_id=customer.id, order_source=ORDER_SOURCE_SALES_AGENT, created_by_staff_id=agent.id)
    plan = SalesPayPlan(name="Standard", created_by_user_id=agent.id)
    period = SalesPayPeriod(month_start=OCT, status="open", is_shadow=False, holidays=[])
    pg_db.session.add_all([order, plan, period])
    pg_db.session.commit()
    version = SalesPayPlanVersion(**_version_fields(plan.id, agent.id, version_no=1, effective_month=OCT))
    pg_db.session.add(version)
    pg_db.session.commit()
    return SimpleNamespace(
        agent_id=agent.id,
        outlet_id=outlet.id,
        order_id=order.id,
        plan_id=plan.id,
        period_id=period.id,
        version_id=version.id,
    )


def _line(w, *, seq, **overrides):
    fields = {
        "agent_user_id": w.agent_id,
        "period_id": w.period_id,
        "kind": "commission_credit",
        "cause": None,
        "order_id": w.order_id,
        "plan_version_id": w.version_id,
        "amount": None,  # v5: a commission line records no money
        "earned_month": OCT,
        "occurred_at": AT,
        "idempotency_key": f"commission:{w.order_id}:{seq}",
        "snapshot": {},
    }
    fields.update(overrides)
    return SalesPayLedgerLine(**fields)


def _check(w, **overrides):
    fields = {
        "outlet_id": w.outlet_id,
        "agent_user_id": w.agent_id,
        "status": "tracking",
        "plan_version_id": w.version_id,
        "activation_reason": "first_order",
        "activated_at": AT,
        "onboarded_at": AT,
        "evidence": {},
    }
    fields.update(overrides)
    return SalesPayOutletCheck(**fields)


def _live_names(pg_db, table):
    """Every CHECK, FK and unique name on ``table``, plus every index name except the primary key's."""
    constraints = pg_db.session.execute(
        text("SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(:table)::oid AND contype IN ('c', 'f', 'u')"),
        {"table": table},
    ).scalars()
    indexes = pg_db.session.execute(
        text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = :table AND indexname <> :pkey"),
        {"table": table, "pkey": f"{table}_pkey"},
    ).scalars()
    return set(constraints) | set(indexes)


def _live_columns(pg_db, table):
    rows = pg_db.session.execute(
        text(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :table"
        ),
        {"table": table},
    ).all()
    return {name: is_nullable == "YES" for name, is_nullable in rows}


def test_every_month_column_refuses_a_mid_month_date(pg_db):
    """§10.5: the first-of-month CHECKs refuse 2026-10-05 on every month column.

    They exist only on Postgres (`ddl_if`), so this is their only proof. Each probe breaks the
    day alone, and the same rows dated the 1st then land.
    """
    w = _pay_world(pg_db)

    def _terms(month):
        return SalesAgentPayTerms(
            agent_user_id=w.agent_id,
            effective_month=month,
            base_salary=Decimal("2600000"),
            plan_id=w.plan_id,
            created_by_user_id=w.agent_id,
        )

    probes = [
        (
            SalesPayPlanVersion(**_version_fields(w.plan_id, w.agent_id, version_no=2, effective_month=MID_OCTOBER)),
            "ck_sales_pay_plan_versions_effective_month_first_of_month",
        ),
        (_terms(MID_OCTOBER), "ck_sales_agent_pay_terms_effective_month_first_of_month"),
        (
            SalesPayPeriod(month_start=MID_OCTOBER, status="open", is_shadow=False, holidays=[]),
            "ck_sales_pay_periods_month_start_first_of_month",
        ),
        (_line(w, seq=1, earned_month=MID_OCTOBER), "ck_sales_pay_ledger_lines_earned_month_first_of_month"),
    ]
    for row, constraint in probes:
        _rollback_on_named_integrity(pg_db, row, constraint)

    pg_db.session.add_all(
        [
            SalesPayPlanVersion(**_version_fields(w.plan_id, w.agent_id, version_no=2, effective_month=NOV)),
            _terms(NOV),
            SalesPayPeriod(month_start=NOV, status="open", is_shadow=False, holidays=[]),
            _line(w, seq=1),
        ]
    )
    pg_db.session.commit()
    assert SalesPayPeriod.query.count() == 2


def test_the_ledger_amount_and_cause_and_the_bonus_window_hold_by_name(pg_db):
    """§10.5 (v5). The migrated `ck_sales_pay_ledger_lines_amount_kind` refuses a commission line with
    an amount and a bonus line without one. `ck_sales_pay_ledger_lines_cause` refuses the retired
    `plan_changed` and, because it spells out `cause IS NOT NULL` (R1-1), a causeless reversal and a
    causeless difference whose amount is NULL. The bonus window refuses an end with no start."""
    w = _pay_world(pg_db)
    bonus = {
        "kind": "new_outlet_bonus",
        "order_id": None,
        "outlet_id": w.outlet_id,
        "idempotency_key": f"new_outlet_bonus:{w.outlet_id}",
    }

    for row, constraint in [
        (_line(w, seq=2, amount=Decimal("9000")), "ck_sales_pay_ledger_lines_amount_kind"),
        (
            _line(w, seq=2, kind="commission_difference", cause="units_reduced", amount=Decimal("-1500")),
            "ck_sales_pay_ledger_lines_amount_kind",
        ),
        (_line(w, seq=2, **bonus), "ck_sales_pay_ledger_lines_amount_kind"),
        (_line(w, seq=2, kind="commission_reversal"), "ck_sales_pay_ledger_lines_cause"),
        (_line(w, seq=2, kind="commission_difference"), "ck_sales_pay_ledger_lines_cause"),
        (_line(w, seq=2, kind="commission_difference", cause="plan_changed"), "ck_sales_pay_ledger_lines_cause"),
    ]:
        _rollback_on_named_integrity(pg_db, row, constraint)
    _rollback_on_named_integrity(pg_db, _check(w, window_end=LATER), "ck_sales_pay_outlet_checks_window_pair")

    pg_db.session.add_all(
        [
            _line(w, seq=1),
            _line(w, seq=2, kind="commission_difference", cause="units_reduced"),
            _line(w, seq=3, kind="commission_difference", cause="units_restored"),
            _line(w, seq=4, **bonus, amount=Decimal("150000")),
            _check(w, window_start=AT, window_end=LATER),
        ]
    )
    pg_db.session.commit()
    assert SalesPayLedgerLine.query.count() == 4
    assert SalesPayOutletCheck.query.count() == 1


def test_a_schedule_holds_one_tier_per_first_unit_by_name(pg_db):
    """§10.5: NULLs are distinct in `uq_sales_pay_plan_rates_version_product_from`, so the partial index
    `uq_sales_pay_plan_rates_version_default_from` is what keeps the default schedule (`product_id`
    NULL) to one tier per first unit. Two products may each start a tier at the same unit, and no
    tier starts before unit 1."""
    w = _pay_world(pg_db)
    water = make_product(pg_db, name="Water 19L", price="20000")
    juice = make_product(pg_db, name="Juice 1L", price="25000", size="5L")

    def tier(product_id, from_unit, value="1500", mode="per_unit"):
        return SalesPayPlanRate(
            plan_version_id=w.version_id,
            product_id=product_id,
            from_unit=from_unit,
            rate_mode=mode,
            rate_value=Decimal(value),
        )

    pg_db.session.add_all(
        [tier(None, 1, "3", "percent"), tier(water.id, 1), tier(juice.id, 1), tier(water.id, 301, "2000")]
    )
    pg_db.session.commit()

    _rollback_on_named_integrity(pg_db, tier(None, 1, "4", "percent"), "uq_sales_pay_plan_rates_version_default_from")
    _rollback_on_named_integrity(pg_db, tier(water.id, 301), "uq_sales_pay_plan_rates_version_product_from")
    _rollback_on_named_integrity(pg_db, tier(juice.id, 0), "ck_sales_pay_plan_rates_from_unit")

    pg_db.session.add(tier(None, 501, "4", "percent"))
    pg_db.session.commit()
    assert SalesPayPlanRate.query.filter_by(plan_version_id=w.version_id, product_id=None).count() == 2


def test_every_migrated_vocabulary_check_lists_exactly_the_live_tuple(pg_db):
    """The migration keeps frozen copies of the tuples; this is where they meet the live ones.

    A value added to a tuple alone would be offered by a service and refused by the database.
    A value added in a migration alone would be accepted by the database and refused by the
    service.
    """
    for name, (table, values) in PAY_VOCABULARY_CHECKS.items():
        constraint_def = _constraint_def(pg_db, table, name)
        assert constraint_def is not None, f"{name} is missing from the migrated schema"
        assert _quoted_literals(constraint_def) == set(values), f"{name}: {constraint_def}"

    # The cause CHECK holds one IN list per kind that needs a cause, plus the causeless kinds.
    cause_def = _constraint_def(pg_db, "sales_pay_ledger_lines", "ck_sales_pay_ledger_lines_cause")
    assert cause_def is not None
    arrays = [set(re.findall(r"'([^']*)'", body)) for body in re.findall(r"ARRAY\[(.*?)\]", cause_def, re.S)]
    assert set(SALES_PAY_REVERSAL_CAUSES) in arrays, cause_def
    assert set(SALES_PAY_DIFFERENCE_CAUSES) in arrays, cause_def


def test_the_migration_carries_exactly_the_models_names_and_columns(pg_db):
    """Model/migration parity, table by table.

    The SQLite suite trusts the models and production trusts the migration. A name spelled
    differently in one of them breaks `downgrade()`, which drops by name. A column nullable in
    one and NOT NULL in the other passes every SQLite test and fails in production.
    """
    for model in NEW_TABLE_MODELS:
        table = model.__tablename__
        declared_names = {
            constraint.name
            for constraint in model.__table__.constraints
            if not isinstance(constraint, PrimaryKeyConstraint)
        } | {index.name for index in model.__table__.indexes}
        declared_columns = {column.name: column.nullable for column in model.__table__.columns}

        assert _live_names(pg_db, table) == declared_names, table
        assert _live_columns(pg_db, table) == declared_columns, table

    profile_names = _live_names(pg_db, "sales_agent_profiles")
    assert {
        "ck_sales_agent_profiles_employment_dates_order",
        "ck_sales_agent_profiles_employment_set_pair",
        "fk_sales_agent_profiles_employment_set_by_user_id",
    } <= profile_names
    profile_columns = _live_columns(pg_db, "sales_agent_profiles")
    assert {
        name: profile_columns.get(name)
        for name in ("employment_start_date", "employment_end_date", "employment_set_by_user_id", "employment_set_at")
    } == {
        "employment_start_date": True,
        "employment_end_date": True,
        "employment_set_by_user_id": True,
        "employment_set_at": True,
    }
    assert _live_columns(pg_db, "sales_agent_day_plans").get("due_outlet_ids") is True
    assert _index_def(pg_db, "ix_orders_order_source_created_by_staff_id") == (
        "CREATE INDEX ix_orders_order_source_created_by_staff_id ON public.orders "
        "USING btree (order_source, created_by_staff_id)"
    )

    # The two profile rules, by name, on the migrated table.
    agent = User(phone="+998900000403", password_hash="not-a-real-hash", first_name="Dilshod")
    pg_db.session.add(agent)
    pg_db.session.commit()
    agent_id = agent.id
    _rollback_on_named_integrity(
        pg_db,
        SalesAgentProfile(
            user_id=agent_id,
            districts=[],
            employment_start_date=date(2026, 10, 1),
            employment_end_date=date(2026, 9, 30),
        ),
        "ck_sales_agent_profiles_employment_dates_order",
    )
    _rollback_on_named_integrity(
        pg_db,
        SalesAgentProfile(user_id=agent_id, districts=[], employment_set_at=LATER),
        "ck_sales_agent_profiles_employment_set_pair",
    )
