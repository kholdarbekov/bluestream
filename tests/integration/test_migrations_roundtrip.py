"""TST-004: Alembic migration upgrade → downgrade → upgrade roundtrip.

Catches the classic rollout-day surprises:
  - ``downgrade()`` drops a column or table that a later (still-applied)
    migration refers to.
  - ``upgrade()`` assumes data that an earlier migration neglected to backfill.
  - ``downgrade()`` is a no-op or raises, so a real rollback would fail.

The test runs against an ephemeral Postgres database created on the configured
``DATABASE_URL`` server. SQLite is rejected because production runs on
Postgres and several migrations use Postgres-only constructs (e.g. JSONB,
``ENCODE(DIGEST(...))``).
"""
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from tests.integration.conftest import transient_pg_database


@pytest.fixture
def ephemeral_pg_database():
    """A transient, EMPTY Postgres database — these tests replay the migrations
    themselves, so they must not get the migrated template conftest hands out."""
    with transient_pg_database("migr_rt") as url:
        yield url


@pytest.mark.integration
def test_migrations_upgrade_downgrade_upgrade(ephemeral_pg_database):
    """``alembic upgrade head → downgrade base → upgrade head`` must succeed."""
    from business_app import create_app
    from flask_migrate import downgrade, upgrade

    app = create_app({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': ephemeral_pg_database,
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'SECRET_KEY': 'test-secret-key-for-migration-roundtrip-32-chars',
        'JWT_SECRET_KEY': 'test-jwt-secret-key-for-migration-roundtrip',
        'CELERY_ALWAYS_EAGER': True,
    })

    with app.app_context():
        upgrade(revision='head')
        downgrade(revision='base')
        upgrade(revision='head')


@pytest.mark.integration
def test_auditeventtype_enum_covers_all_python_members(ephemeral_pg_database):
    """Every ``AuditEventType`` Python member must exist as a label in the
    Postgres ``auditeventtype`` enum once migrated to head.

    Regression (prod): ``order_edited``, ``session_reopened`` and
    ``payment_verification_code_verified`` were added to the Python enum but no
    ``ALTER TYPE ... ADD VALUE`` migration was ever created, so audit inserts
    for those events failed with ``InvalidTextRepresentation`` and the audit
    rows were silently dropped.
    """
    from business_app import create_app, db
    from business_app.models.audit import AuditEventType
    from flask_migrate import upgrade

    app = create_app({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': ephemeral_pg_database,
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'SECRET_KEY': 'test-secret-key-for-migration-roundtrip-32-chars',
        'JWT_SECRET_KEY': 'test-jwt-secret-key-for-migration-roundtrip',
        'CELERY_ALWAYS_EAGER': True,
    })

    with app.app_context():
        upgrade(revision='head')
        pg_labels = set(
            db.session.execute(
                text(
                    "SELECT e.enumlabel FROM pg_enum e "
                    "JOIN pg_type t ON e.enumtypid = t.oid "
                    "WHERE t.typname = 'auditeventtype'"
                )
            ).scalars().all()
        )

    python_values = {member.value for member in AuditEventType}
    missing = python_values - pg_labels
    assert not missing, f"PG auditeventtype enum missing labels: {sorted(missing)}"


# Migration b7e3c1a95d42 (sales-agent pay, spec 2026-09-28 §3.2, §3.3, §3.7): every named
# constraint and index, per table, transcribed from the spec. `downgrade()` drops by these
# names, so a typo surfaces here rather than on rollback day.
PAY_PARENT_REVISION = "a4c7e9b2d5f8"
PAY_V4_REVISION = "b7e3c1a95d42"
PAY_V5_REVISION = "c5d8e2f1a7b3"
PAY_TABLE_NAMES = {
    "sales_pay_plans": {
        "fk_sales_pay_plans_created_by_user_id",
        "uq_sales_pay_plans_name",
    },
    "sales_pay_plan_versions": {
        "fk_sales_pay_plan_versions_plan_id",
        "fk_sales_pay_plan_versions_created_by_user_id",
        "uq_sales_pay_plan_versions_plan_version",
        "ck_sales_pay_plan_versions_default_rate_mode",
        "ck_sales_pay_plan_versions_default_rate_value",
        "ck_sales_pay_plan_versions_ranges",
        "ck_sales_pay_plan_versions_amounts_whole",
        "ck_sales_pay_plan_versions_effective_month_first_of_month",
        "ix_sales_pay_plan_versions_plan_month",
    },
    "sales_pay_plan_rates": {
        "fk_sales_pay_plan_rates_plan_version_id",
        "fk_sales_pay_plan_rates_product_id",
        "uq_sales_pay_plan_rates_version_product",
        "ck_sales_pay_plan_rates_rate_mode",
        "ck_sales_pay_plan_rates_rate_value",
    },
    "sales_agent_pay_terms": {
        "fk_sales_agent_pay_terms_agent_user_id",
        "fk_sales_agent_pay_terms_plan_id",
        "fk_sales_agent_pay_terms_created_by_user_id",
        "ck_sales_agent_pay_terms_base_nonneg",
        "ck_sales_agent_pay_terms_base_salary_whole",
        "ck_sales_agent_pay_terms_effective_month_first_of_month",
        "ix_sales_agent_pay_terms_agent_month",
    },
    "sales_pay_periods": {
        "fk_sales_pay_periods_closed_by_user_id",
        "fk_sales_pay_periods_approved_by_user_id",
        "fk_sales_pay_periods_paid_by_user_id",
        "uq_sales_pay_periods_month_start",
        "ck_sales_pay_periods_status",
        "ck_sales_pay_periods_closed_stamp",
        "ck_sales_pay_periods_approved_stamp",
        "ck_sales_pay_periods_paid_stamp",
        "ck_sales_pay_periods_month_start_first_of_month",
    },
    "sales_agent_unpaid_days": {
        "fk_sales_agent_unpaid_days_agent_user_id",
        "fk_sales_agent_unpaid_days_created_by_user_id",
        "uq_sales_agent_unpaid_days_agent_date",
    },
    "sales_pay_ledger_lines": {
        "fk_sales_pay_ledger_lines_agent_user_id",
        "fk_sales_pay_ledger_lines_period_id",
        "fk_sales_pay_ledger_lines_order_id",
        "fk_sales_pay_ledger_lines_outlet_id",
        "fk_sales_pay_ledger_lines_plan_version_id",
        "uq_sales_pay_ledger_lines_idempotency_key",
        "ck_sales_pay_ledger_lines_kind",
        "ck_sales_pay_ledger_lines_cause",
        "ck_sales_pay_ledger_lines_subject",
        "ck_sales_pay_ledger_lines_amount_sign",
        "ck_sales_pay_ledger_lines_amount_whole",
        "ck_sales_pay_ledger_lines_earned_month_first_of_month",
        "ix_sales_pay_ledger_lines_agent_period",
        "ix_sales_pay_ledger_lines_order_id",
        "ix_sales_pay_ledger_lines_outlet_id",
    },
    "sales_pay_outlet_checks": {
        "fk_sales_pay_outlet_checks_outlet_id",
        "fk_sales_pay_outlet_checks_agent_user_id",
        "fk_sales_pay_outlet_checks_plan_version_id",
        "fk_sales_pay_outlet_checks_activation_actor_user_id",
        "fk_sales_pay_outlet_checks_ledger_line_id",
        "uq_sales_pay_outlet_checks_outlet_id",
        "ck_sales_pay_outlet_checks_status",
        "ck_sales_pay_outlet_checks_qualified",
        "ck_sales_pay_outlet_checks_window_pair",
    },
    "sales_pay_penalty_types": {
        "fk_sales_pay_penalty_types_created_by_user_id",
        "fk_sales_pay_penalty_types_updated_by_user_id",
        "ck_sales_pay_penalty_types_default_amount_positive",
        "ck_sales_pay_penalty_types_default_amount_whole",
    },
    "sales_pay_penalties": {
        "fk_sales_pay_penalties_agent_user_id",
        "fk_sales_pay_penalties_penalty_type_id",
        "fk_sales_pay_penalties_period_id",
        "fk_sales_pay_penalties_proposed_by_user_id",
        "fk_sales_pay_penalties_decided_by_user_id",
        "fk_sales_pay_penalties_cancelled_by_user_id",
        "ck_sales_pay_penalties_status",
        "ck_sales_pay_penalties_origin",
        "ck_sales_pay_penalties_booked",
        "ck_sales_pay_penalties_decided",
        "ck_sales_pay_penalties_cancelled",
        "ck_sales_pay_penalties_amount_positive",
        "ck_sales_pay_penalties_amount_whole",
        "ck_sales_pay_penalties_direct_confirmed",
        "ix_sales_pay_penalties_agent_period",
        "ix_sales_pay_penalties_status",
    },
    "sales_pay_adjustments": {
        "fk_sales_pay_adjustments_agent_user_id",
        "fk_sales_pay_adjustments_period_id",
        "fk_sales_pay_adjustments_carried_from_period_id",
        "fk_sales_pay_adjustments_created_by_user_id",
        "uq_sales_pay_adjustments_carry_from_agent",
        "ck_sales_pay_adjustments_amount_nonzero",
        "ck_sales_pay_adjustments_amount_whole",
        "ck_sales_pay_adjustments_source",
        "ck_sales_pay_adjustments_carry",
        "ck_sales_pay_adjustments_carry_sign",
    },
    "sales_pay_statements": {
        "fk_sales_pay_statements_period_id",
        "fk_sales_pay_statements_agent_user_id",
        "fk_sales_pay_statements_terms_id",
        "fk_sales_pay_statements_plan_version_id",
        "fk_sales_pay_statements_computed_by_user_id",
        "fk_sales_pay_statements_owed_in_statement_id",
        "uq_sales_pay_statements_period_agent",
        "uq_sales_pay_statements_owed_in_statement_id",
        "ck_sales_pay_statements_amounts_whole",
        "ck_sales_pay_statements_nonneg",
        "ck_sales_pay_statements_total",
        "ck_sales_pay_statements_owed_in",
        "ck_sales_pay_statements_days",
        "ck_sales_pay_statements_gate",
    },
    "agent_order_approvals": {
        "fk_agent_order_approvals_order_id",
        "fk_agent_order_approvals_outlet_id",
        "fk_agent_order_approvals_agent_user_id",
        "fk_agent_order_approvals_decided_by_user_id",
        "uq_agent_order_approvals_order_id",
        "ck_agent_order_approvals_status",
        "ck_agent_order_approvals_decided",
        "ck_agent_order_approvals_decider",
        "ck_agent_order_approvals_reason",
        "ix_agent_order_approvals_status_requested_at",
        "ix_agent_order_approvals_agent_user_id",
    },
}
# Migration c5d8e2f1a7b3 (v5 tiers, spec §3.5b): head's names where they differ from b7e3c1a95d42.
# PAY_TABLE_NAMES stays the v4 set: the head -> b7e3c1a95d42 leg asserts it.
PAY_HEAD_TABLE_NAMES = {
    **PAY_TABLE_NAMES,
    "sales_pay_plan_versions": PAY_TABLE_NAMES["sales_pay_plan_versions"]
    - {"ck_sales_pay_plan_versions_default_rate_mode", "ck_sales_pay_plan_versions_default_rate_value"},
    "sales_pay_plan_rates": {
        "fk_sales_pay_plan_rates_plan_version_id",
        "fk_sales_pay_plan_rates_product_id",
        "uq_sales_pay_plan_rates_version_product_from",
        "uq_sales_pay_plan_rates_version_default_from",
        "ck_sales_pay_plan_rates_from_unit",
        "ck_sales_pay_plan_rates_rate_mode",
        "ck_sales_pay_plan_rates_rate_value",
    },
    # c5d8e2f1a7b3's ledger part: commission lines carry no amount.
    "sales_pay_ledger_lines": (PAY_TABLE_NAMES["sales_pay_ledger_lines"] - {"ck_sales_pay_ledger_lines_amount_sign"})
    | {"ck_sales_pay_ledger_lines_amount_kind"},
}
PAY_EXISTING_TABLE_NAMES = {
    "sales_agent_profiles": {
        "ck_sales_agent_profiles_employment_dates_order",
        "ck_sales_agent_profiles_employment_set_pair",
        "fk_sales_agent_profiles_employment_set_by_user_id",
    },
    "orders": {"ix_orders_order_source_created_by_staff_id"},
}
PAY_EXISTING_COLUMNS = {
    "sales_agent_profiles": {
        "employment_start_date",
        "employment_end_date",
        "employment_set_by_user_id",
        "employment_set_at",
    },
    "sales_agent_day_plans": {"due_outlet_ids"},
}


def _schema_names(conn, table):
    """CHECK, FK and unique names on ``table``, plus its index names except the primary key's."""
    constraints = conn.execute(
        text("SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(:t)::oid AND contype IN ('c', 'f', 'u')"),
        {"t": table},
    ).scalars()
    indexes = conn.execute(
        text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t AND indexname <> :pkey"),
        {"t": table, "pkey": f"{table}_pkey"},
    ).scalars()
    return set(constraints) | set(indexes)


def _schema_columns(conn, table):
    return set(
        conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :t"),
            {"t": table},
        ).scalars()
    )


def _assert_pay_schema(engine, *, present, names=PAY_HEAD_TABLE_NAMES):
    with engine.connect() as conn:
        for table, expected in names.items():
            assert _schema_names(conn, table) == (expected if present else set()), table
            exists = conn.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar() is not None
            assert exists is present, table
        for table, names_ in PAY_EXISTING_TABLE_NAMES.items():
            found = _schema_names(conn, table) & names_
            assert found == (names_ if present else set()), table
        for table, columns in PAY_EXISTING_COLUMNS.items():
            found = _schema_columns(conn, table) & columns
            assert found == (columns if present else set()), table


@pytest.mark.integration
def test_sales_agent_pay_migration_roundtrips_with_every_name(ephemeral_pg_database):
    """upgrade → downgrade to the previous head → upgrade (spec §10.5), with head's names asserted.

    Two pay migrations now sit above a4c7e9b2d5f8 (b7e3c1a95d42, then c5d8e2f1a7b3's tiers). On an
    empty database the v5 downgrade's refusals do not fire. b7e3c1a95d42's downgrade then drops the
    13 tables, the orders index, `due_outlet_ids` and the four profile columns. The second upgrade
    proves nothing was left behind that would collide.
    """
    from business_app import create_app, db
    from flask_migrate import downgrade, upgrade

    app = create_app({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': ephemeral_pg_database,
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'SECRET_KEY': 'test-secret-key-for-migration-roundtrip-32-chars',
        'JWT_SECRET_KEY': 'test-jwt-secret-key-for-migration-roundtrip',
        'CELERY_ALWAYS_EAGER': True,
    })

    with app.app_context():
        upgrade(revision='head')
        _assert_pay_schema(db.engine, present=True)
        downgrade(revision=PAY_PARENT_REVISION)
        _assert_pay_schema(db.engine, present=False)
        upgrade(revision='head')
        _assert_pay_schema(db.engine, present=True)


def _migration_app(database_url):
    from business_app import create_app

    return create_app({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': database_url,
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'SECRET_KEY': 'test-secret-key-for-migration-roundtrip-32-chars',
        'JWT_SECRET_KEY': 'test-jwt-secret-key-for-migration-roundtrip',
        'CELERY_ALWAYS_EAGER': True,
    })


def _rows(engine, sql, **params):
    """`sql`'s rows as tuples, on a connection of its own: no session may hold a lock that a migration's
    ALTER TABLE then waits for."""
    with engine.connect() as conn:
        return [tuple(row) for row in conn.execute(text(sql), params)]


def _run(engine, sql):
    with engine.begin() as conn:
        conn.execute(text(sql))


def _add(db, row):
    db.session.add(row)
    db.session.commit()
    db.session.remove()


def _insert_user(db, **columns):
    """A `users` row written with Core, and its id. For fixtures staged BEFORE b4f1c7e9a2d3.

    The ORM sends every mapped column, `None` included (SQLAlchemy's legacy insert behaviour), so an
    ORM `User` fails at any revision that predates a column the model has since gained. Core sends
    only what is given plus the column defaults, so the row lands at the older schema too.
    """
    from business_app.models.user import User

    user_id = db.session.execute(User.__table__.insert().values(**columns)).inserted_primary_key[0]
    db.session.commit()
    return user_id


def _insert_order(db, **columns):
    """An `orders` row written with Core, and its id, for the same reason as `_insert_user`: fixtures
    staged BEFORE 3f8b2d6a9c41, which predates `orders.online_payable_since`. Core skips the model's
    order-number hook, so the caller names the number."""
    from business_app.models.order import Order

    values = {"subtotal": Decimal("15000.00"), "total_amount": Decimal("15000.00"), **columns}
    order_id = db.session.execute(Order.__table__.insert().values(**values)).inserted_primary_key[0]
    db.session.commit()
    return order_id


def _revision(engine):
    return _rows(engine, "SELECT version_num FROM alembic_version")[0][0]


@pytest.mark.integration
def test_pay_v5_migration_roundtrips_through_the_v4_schema(ephemeral_pg_database):
    """head → b7e3c1a95d42 → head (spec §3.5b, §10.7). On an empty database the v5 downgrade's refusals
    do not fire. The v4 names and the two default-rate columns come back and `from_unit` goes; the
    second upgrade lands on head's names again."""
    from flask_migrate import downgrade, upgrade

    from business_app import db

    app = _migration_app(ephemeral_pg_database)
    with app.app_context():
        upgrade(revision='head')
        downgrade(revision=PAY_V4_REVISION)
        _assert_pay_schema(db.engine, present=True, names=PAY_TABLE_NAMES)
        with db.engine.connect() as conn:
            assert {"default_rate_mode", "default_rate_value"} <= _schema_columns(conn, "sales_pay_plan_versions")
            assert "from_unit" not in _schema_columns(conn, "sales_pay_plan_rates")
        upgrade(revision='head')
        _assert_pay_schema(db.engine, present=True)
        with db.engine.connect() as conn:
            assert {"default_rate_mode", "default_rate_value"}.isdisjoint(
                _schema_columns(conn, "sales_pay_plan_versions")
            )
            assert "from_unit" in _schema_columns(conn, "sales_pay_plan_rates")


# ---- T-TIER-17 (spec §10.3 is its one definition; §3.5b, §11.5) ----

OCT, NOV, DEC = date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1)
AT = datetime(2026, 10, 4, 9, 12, tzinfo=UTC)
_V4_VERSION = (
    "INSERT INTO sales_pay_plan_versions (plan_id, version_no, effective_month, default_rate_mode, "
    "default_rate_value, gate_bands, gate_min_visits_due, bonus_amount, bonus_window_days, "
    "bonus_min_orders_with_total, bonus_min_combined_total, bonus_min_orders_any_amount, "
    "bonus_prior_customer_lookback_days, created_by_user_id, created_at) VALUES (:plan_id, :version_no, "
    ":month, :mode, :value, CAST(:bands AS JSON), 20, 150000, 60, 2, 300000, 5, 180, :actor, now()) RETURNING id"
)
_V4_RATE = (
    "INSERT INTO sales_pay_plan_rates (plan_version_id, product_id, rate_mode, rate_value, created_at) "
    "VALUES (:version_id, :product_id, 'per_unit', :value, now())"
)
_V4_BANDS = json.dumps([{"min_pct": "0.0", "multiplier": "1.000"}])
# Spec §11.5's pre-step: with no statement, commission lines are trial data the sync rebuilds.
_DELETE_COMMISSION_LINES = "DELETE FROM sales_pay_ledger_lines WHERE kind <> 'new_outlet_bonus'"


def _v4_plan_world(db):
    """T-TIER-17's fixture at b7e3c1a95d42, mirroring dev: plan "Standard" with v1 (October, 3% by
    default, 19 L at 1,500 per unit) and v2 (November, 400 per unit by default, 19 L at 500), an agent
    with October terms, October open, and one agent order. The version and rate tables are v4-shaped
    here, so they are written in SQL. The other tables are the same in both schemas."""
    from business_app.models.sales import Outlet
    from business_app.models.sales_pay import SalesAgentPayTerms, SalesPayPeriod, SalesPayPlan
    from business_app.utils.constants import ORDER_SOURCE_SALES_AGENT
    from tests.integration.sales_pay_builders import make_product

    # Users and the order go in with Core: this schema predates users.telegram_blocked_at and
    # orders.online_payable_since (see `_insert_user`).
    agent_id = _insert_user(
        db, phone="+998900000501", password_hash="not-a-real-hash", first_name="Sardor", last_name="Agent"
    )
    customer_id = _insert_user(db, phone="+998900000502", password_hash="not-a-real-hash", first_name="Store")
    db.session.add(Outlet(name="Tier Shop", outlet_type="grocery_store", stage="active"))
    db.session.commit()
    water = make_product(db, name="Water 19L", price="20000")
    plan = SalesPayPlan(name="Standard", created_by_user_id=agent_id)
    period = SalesPayPeriod(month_start=OCT, status="open", is_shadow=False, holidays=[])
    order_id = _insert_order(
        db, order_number="SA_000001_26", user_id=customer_id, order_source=ORDER_SOURCE_SALES_AGENT,
        created_by_staff_id=agent_id,
    )
    db.session.add_all([plan, period])
    db.session.commit()
    terms = SalesAgentPayTerms(
        agent_user_id=agent_id,
        effective_month=OCT,
        base_salary=Decimal("2600000"),
        plan_id=plan.id,
        created_by_user_id=agent_id,
    )
    db.session.add(terms)
    db.session.commit()
    w = SimpleNamespace(
        agent=agent_id, water=water.id, plan=plan.id, period=period.id, order=order_id, terms=terms.id
    )
    db.session.remove()
    versions = []
    with db.engine.begin() as conn:
        for version_no, month, mode, value, water_rate in (
            (1, OCT, "percent", "3", "1500"),
            (2, NOV, "per_unit", "400", "500"),
        ):
            version_id = conn.execute(
                text(_V4_VERSION),
                {"plan_id": w.plan, "version_no": version_no, "month": month, "mode": mode, "value": Decimal(value),
                 "bands": _V4_BANDS, "actor": w.agent},
            ).scalar_one()
            conn.execute(
                text(_V4_RATE), {"version_id": version_id, "product_id": w.water, "value": Decimal(water_rate)}
            )
            versions.append(version_id)
    w.v1, w.v2 = versions
    return w


def _commission_line(w, *, amount):
    from business_app.models.sales_pay import SalesPayLedgerLine

    return SalesPayLedgerLine(
        agent_user_id=w.agent,
        period_id=w.period,
        kind="commission_credit",
        cause=None,
        order_id=w.order,
        plan_version_id=w.v1,
        amount=amount,
        earned_month=OCT,
        occurred_at=AT,
        idempotency_key=f"commission:{w.order}:1",
        snapshot={},
    )


def _statement(w):
    from business_app.models.sales_pay import SalesPayStatement

    zero = Decimal("0")
    return SalesPayStatement(
        period_id=w.period, agent_user_id=w.agent, terms_id=w.terms, plan_version_id=w.v1, revision=1,
        computed_at=AT, computed_by_user_id=w.agent, base_salary=Decimal("2600000"), working_days=31,
        worked_days=31, base_amount=Decimal("2600000"), commission_amount=zero, late_commission_amount=zero,
        gate_visits_due=0, gate_visits_counted=0, gate_compliance_pct=None, gate_multiplier=Decimal("1.000"),
        gated_commission_amount=zero, bonus_amount=zero, penalty_amount=zero, adjustment_amount=zero,
        variable_amount=zero, carry_in_amount=zero, owed_in_statement_id=None, gross_amount=Decimal("2600000"),
        total_amount=Decimal("2600000"), carry_out_amount=zero, owed_amount=zero, inputs={},
    )


@pytest.mark.integration
def test_t_tier_17_backfills_single_tiers_refuses_live_commission_and_rolls_back_to_the_first_tier(
    ephemeral_pg_database,
):
    """T-TIER-17, on dev's two versions.

    - At b7e3c1a95d42, one commission line makes the upgrade refuse, and nothing is written.
    - After §11.5's DELETE the upgrade backfills four tier rows at unit 1, two of them the version
      defaults (product_id NULL). Each version resolves to single-tier schedules of its v4 rates, and
      the version table has no default-rate column.
    - The downgrade refuses while a commission line exists, then while a statement exists.
    - With neither, it puts each default back into the v4 columns and each product's FIRST tier back
      as its v4 row. v3's two-tier schedules keep 800 and 1,000 and lose 900 and 1,500 (lossy, §3.5b).
    - pay v5 (c5d8e2f1a7b3) → a4c7e9b2d5f8 then passes.
    """
    from alembic import command

    from business_app import db
    from business_app.models.sales_pay import SalesPayPlanRate, SalesPayPlanVersion
    from business_app.services.sales.pay_plan_service import SalesPayPlanService
    from business_app.services.sales.pay_rules import Rate, Tier, TierSchedule
    from tests.unit.test_sales_pay_models import _version_fields

    def single(mode, value):
        return TierSchedule(tiers=(Tier(from_unit=1, rate=Rate(mode, Decimal(value))),))

    app = _migration_app(ephemeral_pg_database)
    with app.app_context():
        config = app.extensions["migrate"].migrate.get_config()
        command.upgrade(config, PAY_V4_REVISION)
        w = _v4_plan_world(db)

        _add(db, _commission_line(w, amount=Decimal("9000")))
        with pytest.raises(RuntimeError, match=r"pay v5: 1 commission ledger lines and 0 statements exist"):
            command.upgrade(config, PAY_V5_REVISION)
        assert _revision(db.engine) == PAY_V4_REVISION
        assert _rows(db.engine, "SELECT count(*) FROM sales_pay_plan_rates") == [(2,)]
        with db.engine.connect() as conn:
            assert "from_unit" not in _schema_columns(conn, "sales_pay_plan_rates")

        _run(db.engine, _DELETE_COMMISSION_LINES)
        command.upgrade(config, PAY_V5_REVISION)

        assert _rows(
            db.engine,
            "SELECT plan_version_id, product_id, from_unit, rate_mode, rate_value FROM sales_pay_plan_rates "
            "ORDER BY plan_version_id, product_id NULLS FIRST",
        ) == [
            (w.v1, None, 1, "percent", Decimal("3.00")),
            (w.v1, w.water, 1, "per_unit", Decimal("1500.00")),
            (w.v2, None, 1, "per_unit", Decimal("400.00")),
            (w.v2, w.water, 1, "per_unit", Decimal("500.00")),
        ]
        with db.engine.connect() as conn:
            assert {"default_rate_mode", "default_rate_value"}.isdisjoint(
                _schema_columns(conn, "sales_pay_plan_versions")
            )
        v1, v2 = (SalesPayPlanService.resolve_version(version_id).config for version_id in (w.v1, w.v2))
        assert (v1.default_tiers, v1.product_tiers) == (single("percent", "3"), {w.water: single("per_unit", "1500")})
        assert (v2.default_tiers, v2.product_tiers) == (single("per_unit", "400"), {w.water: single("per_unit", "500")})
        db.session.remove()

        _add(db, _commission_line(w, amount=None))
        with pytest.raises(RuntimeError, match=r"pay v5 downgrade: 1 commission ledger lines and 0 statements exist"):
            command.downgrade(config, PAY_V4_REVISION)
        _run(db.engine, _DELETE_COMMISSION_LINES)
        _add(db, _statement(w))
        with pytest.raises(RuntimeError, match=r"pay v5 downgrade: 0 commission ledger lines and 1 statements exist"):
            command.downgrade(config, PAY_V4_REVISION)
        assert _revision(db.engine) == PAY_V5_REVISION
        _run(db.engine, "DELETE FROM sales_pay_statements")

        v3 = SalesPayPlanVersion(**_version_fields(w.plan, w.agent, version_no=3, effective_month=DEC))
        db.session.add(v3)
        db.session.flush()
        db.session.add_all(
            [
                SalesPayPlanRate(
                    plan_version_id=v3.id, product_id=product_id, from_unit=from_unit, rate_mode="per_unit",
                    rate_value=Decimal(value),
                )
                for product_id, from_unit, value in ((None, 1, "800"), (None, 1001, "900"), (w.water, 1, "1000"),
                                                     (w.water, 301, "1500"))
            ]
        )
        db.session.commit()
        v3_id = v3.id
        db.session.remove()

        command.downgrade(config, PAY_V4_REVISION)

        assert _rows(
            db.engine,
            "SELECT id, default_rate_mode, default_rate_value FROM sales_pay_plan_versions ORDER BY version_no",
        ) == [
            (w.v1, "percent", Decimal("3.00")),
            (w.v2, "per_unit", Decimal("400.00")),
            (v3_id, "per_unit", Decimal("800.00")),
        ]
        assert _rows(
            db.engine,
            "SELECT plan_version_id, product_id, rate_mode, rate_value FROM sales_pay_plan_rates "
            "ORDER BY plan_version_id",
        ) == [
            (w.v1, w.water, "per_unit", Decimal("1500.00")),
            (w.v2, w.water, "per_unit", Decimal("500.00")),
            (v3_id, w.water, "per_unit", Decimal("1000.00")),
        ]

        command.upgrade(config, PAY_V5_REVISION)
        command.downgrade(config, PAY_PARENT_REVISION)
        _assert_pay_schema(db.engine, present=False)


CONTRACT_PRICE_PARENT_REVISION = "c5d8e2f1a7b3"


@pytest.mark.integration
def test_contract_price_split_copies_the_price_into_both_methods_and_rolls_back(ephemeral_pg_database):
    """Spec 2026-10-09 §3.2. Upgrade: every row carries its old price in both columns. Downgrade:
    bank-transfer price wins, then cash, then the product's base price."""
    from alembic import command

    from business_app import db
    from business_app.models.corporate import CorporateContract, CorporateContractStatus
    from business_app.models.product import Product, ProductCategory
    from shared.enums import UserRole, UserType

    app = _migration_app(ephemeral_pg_database)
    with app.app_context():
        config = app.extensions["migrate"].migrate.get_config()
        command.upgrade(config, CONTRACT_PRICE_PARENT_REVISION)

        # Only corporate_contract_product_prices and users.telegram_blocked_at differ between the
        # parent and head, so the ORM is safe for every other table; the price row goes in as raw SQL
        # and the user as Core (see `_insert_user`).
        user_id = _insert_user(
            db, email="split@example.com", phone="+998901110001", password_hash="x" * 60, first_name="S",
            last_name="P", user_type=UserType.ENTITY, role=UserRole.CUSTOMER, is_verified=True,
        )
        category = ProductCategory(name="Water", description="d", is_active=True)
        db.session.add(category)
        db.session.flush()
        product = Product(
            name="Water 19L", description="d", category_id=category.id, size="19L", volume=19.0,
            volume_unit="L", base_price=Decimal("15000.00"), stock_quantity=10, min_stock_level=1,
            max_stock_level=100, is_active=True,
        )
        contract = CorporateContract(
            user_id=user_id, contract_number="SPLIT-1", name="Split",
            status=CorporateContractStatus.ACTIVE, currency="UZS", is_active=True,
        )
        db.session.add_all([product, contract])
        db.session.commit()
        contract_id, product_id = contract.id, product.id
        db.session.remove()

        _run(
            db.engine,
            "INSERT INTO corporate_contract_product_prices "
            "(contract_id, product_id, unit_price, is_prepayment_eligible, is_active, created_at, updated_at) "
            f"VALUES ({contract_id}, {product_id}, 18000.00, true, true, now(), now())",
        )

        command.upgrade(config, "head")
        assert _rows(
            db.engine, "SELECT business_account_unit_price, cash_unit_price FROM corporate_contract_product_prices"
        ) == [(Decimal("18000.00"), Decimal("18000.00"))]
        with db.engine.connect() as conn:
            assert "unit_price" not in _schema_columns(conn, "corporate_contract_product_prices")

        _run(db.engine, "UPDATE corporate_contract_product_prices SET business_account_unit_price = NULL, cash_unit_price = 16000")
        command.downgrade(config, CONTRACT_PRICE_PARENT_REVISION)
        assert _rows(db.engine, "SELECT unit_price FROM corporate_contract_product_prices") == [(Decimal("16000.00"),)]

        command.upgrade(config, "head")
        _run(db.engine, "UPDATE corporate_contract_product_prices SET business_account_unit_price = NULL, cash_unit_price = NULL")
        command.downgrade(config, CONTRACT_PRICE_PARENT_REVISION)
        assert _rows(db.engine, "SELECT unit_price FROM corporate_contract_product_prices") == [(Decimal("15000.00"),)]

        command.upgrade(config, "head")


REORDER_REMINDERS_REVISION = "b4f1c7e9a2d3"
REORDER_REMINDERS_PARENT_REVISION = "e7b2c4d9a1f6"
REORDER_REMINDER_TABLES = ("reorder_reminder_episodes", "reorder_reminder_events", "reorder_reminder_settings")


@pytest.mark.integration
def test_reorder_reminders_downgrade_removes_exactly_what_the_upgrade_added(ephemeral_pg_database):
    """Spec 2026-10-09 §10. The parent has none of the three tables nor `users.telegram_blocked_at`; the
    revision has all four; the second upgrade brings them back. (The base roundtrip above only proves
    the downgrade runs, not that it took the right objects with it.)"""
    from alembic import command

    from business_app import db

    def present():
        with db.engine.connect() as conn:
            return (
                [bool(_schema_columns(conn, table)) for table in REORDER_REMINDER_TABLES],
                "telegram_blocked_at" in _schema_columns(conn, "users"),
            )

    app = _migration_app(ephemeral_pg_database)
    with app.app_context():
        config = app.extensions["migrate"].migrate.get_config()
        command.upgrade(config, REORDER_REMINDERS_REVISION)
        assert present() == ([True, True, True], True)

        command.downgrade(config, REORDER_REMINDERS_PARENT_REVISION)
        assert present() == ([False, False, False], False)
        assert _revision(db.engine) == REORDER_REMINDERS_PARENT_REVISION

        command.upgrade(config, REORDER_REMINDERS_REVISION)
        assert present() == ([True, True, True], True)


# ---- Subscriptions: one frequency, local delivery DATEs (spec 2026-10-23 §4.1, §9; plan T1) ----

SUBSCRIPTION_FREQUENCY_REVISION = "5ea02acb0f0b"
SUBSCRIPTION_FREQUENCY_PARENT_REVISION = "b4f1c7e9a2d3"
_OLD_SUBSCRIPTION = (
    "INSERT INTO subscriptions (subscription_number, user_id, status, name, billing_cycle, billing_amount, "
    "next_billing_date, delivery_frequency, delivery_day_of_week, delivery_day_of_month, delivery_time_slot_id, "
    "delivery_address_id, next_delivery_date, last_delivery_date, start_date, payment_method, discount_percentage, "
    "created_at, updated_at) VALUES (:number, :user_id, 'active', 'Water', :billing, 30000, :next_billing, "
    ":delivery, :dow, :dom, :slot, :address, :next_delivery, :last_delivery, '2026-09-01T00:00:00+00', 'cash', 0, "
    "now(), now()) RETURNING id"
)


def _subscription_columns(conn):
    return {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            text(
                "SELECT column_name, udt_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'subscriptions'"
            )
        )
    }


def _old_subscription_world(db):
    """Six rows at b4f1c7e9a2d3, one per case the backfill must get right. Subscriptions go in as SQL
    (the model is already head-shaped); users and orders with Core (`_insert_user`, `_insert_order`); the
    rest with the ORM, whose tables are the same at both revisions. Tashkent is UTC+5, so a 19:00Z-or-later
    instant is the NEXT local day: the cases put instants there to fail a UTC-date implementation."""
    from business_app.models.delivery import DeliveryTimeSlot
    from business_app.models.user import UserAddress

    user_id = _insert_user(db, phone="+998900000601", password_hash="not-a-real-hash", first_name="Sub")
    address = UserAddress(
        user_id=user_id, full_address="Amir Temur 1", city="Tashkent", latitude=41.31, longitude=69.27
    )
    slot = DeliveryTimeSlot(name="MWF", start_time="10:00", end_time="12:00", is_active=True, available_days=[0, 2, 4])
    db.session.add_all([address, slot])
    db.session.commit()
    w = SimpleNamespace(user=user_id, address=address.id, slot=slot.id)
    db.session.remove()

    rows = {
        # Mismatched pair, no day; billing instant 20:30Z = 01:30 on 1 November in Tashkent.
        "mismatched": dict(billing="monthly", delivery="weekly", next_billing="2026-10-31T20:30:00+00"),
        # Weekly Thursday, a stale next_delivery_date the backfill must replace; billed Monday 26 October.
        "weekly_thu": dict(billing="weekly", delivery="weekly", dow=3, next_billing="2026-10-26T09:00:00+00",
                           next_delivery="2026-10-01T10:00:00+00"),
        # Biweekly with no day: the day comes from the backfilled date (Wednesday 28 October).
        "biweekly_no_day": dict(billing="biweekly", delivery="biweekly", next_billing="2026-10-28T06:00:00+00"),
        # Daily on a Mon/Wed/Fri slot, billed on a Tuesday: the next delivery is Wednesday.
        "daily_slot": dict(billing="daily", delivery="daily", slot=w.slot, next_billing="2026-10-27T04:00:00+00"),
        # Monthly on the 31st, billed 5 November: 30 November.
        "monthly_31": dict(billing="monthly", delivery="monthly", dom=31, next_billing="2026-11-05T07:00:00+00"),
        # Weekly Monday created by an admin (a `created` log), billed 19:30Z = 00:30 Monday 26 October locally;
        # a stored last_delivery_date (21:00Z = 11 October locally) and no orders.
        "admin_monday": dict(billing="weekly", delivery="weekly", dow=0, next_billing="2026-10-25T19:30:00+00",
                             last_delivery="2026-10-10T21:00:00+00"),
    }
    with db.engine.begin() as conn:
        for number, (key, row) in enumerate(rows.items()):
            params = {"dow": None, "dom": None, "slot": None, "next_delivery": None, "last_delivery": None, **row}
            setattr(w, key, conn.execute(
                text(_OLD_SUBSCRIPTION),
                {"number": f"SUB-MIG-{number}", "user_id": w.user, "address": w.address, **params},
            ).scalar_one())
        conn.execute(
            text("INSERT INTO subscription_logs (subscription_id, action, details, user_id, extra_data, created_at, "
                 "updated_at) VALUES (:sid, 'created', 'Created by admin', :uid, '{}', now(), now())"),
            {"sid": w.admin_monday, "uid": w.user},
        )

    # weekly_thu: the NEWEST order (by created_at) has no delivery date, so its local creation day counts
    # (19:00Z on 19 October = 20 October in Tashkent), not the older order's 15 October.
    # biweekly_no_day: one order, delivered on its own delivery_date.
    for number, (subscription_id, delivery_date, created_at) in enumerate((
        (w.weekly_thu, date(2026, 10, 15), datetime(2026, 10, 14, 8, 0, tzinfo=UTC)),
        (w.weekly_thu, None, datetime(2026, 10, 19, 20, 0, tzinfo=UTC)),
        (w.biweekly_no_day, date(2026, 10, 14), datetime(2026, 10, 13, 5, 0, tzinfo=UTC)),
    ), 1):
        _insert_order(db, order_number=f"WB_00000{number}_26", user_id=w.user, subscription_id=subscription_id,
                      delivery_date=delivery_date, created_at=created_at)
    db.session.remove()
    return w


_SUBSCRIPTION_STATE = (
    "SELECT id, frequency::text, billing_cycle::text, delivery_frequency::text, delivery_day_of_week, "
    "delivery_day_of_month, next_delivery_date, last_delivery_date FROM subscriptions ORDER BY id"
)


def _expected_upgraded(w):
    return [
        (w.mismatched, "monthly", "monthly", "weekly", None, 1, date(2026, 11, 1), None),
        (w.weekly_thu, "weekly", "weekly", "weekly", 3, None, date(2026, 10, 29), date(2026, 10, 20)),
        (w.biweekly_no_day, "biweekly", "biweekly", "biweekly", 2, None, date(2026, 10, 28), date(2026, 10, 14)),
        (w.daily_slot, "daily", "daily", "daily", None, None, date(2026, 10, 28), None),
        (w.monthly_31, "monthly", "monthly", "monthly", None, 31, date(2026, 11, 30), None),
        (w.admin_monday, "weekly", "weekly", "weekly", 0, None, date(2026, 10, 26), date(2026, 10, 11)),
    ]


@pytest.mark.integration
def test_subscription_frequency_backfill_and_round_trip(ephemeral_pg_database, capsys):
    """Upgrade: `frequency` from `billing_cycle` (mismatched pairs keep the cadence they were billed on,
    §2.6) and NOT NULL; the delivery dates become local DATEs; next_delivery_date is the first anchor on or
    after next_billing_date's local day; NULL weekly/biweekly/monthly days come from it; last_delivery_date
    is the newest order's. The pre-flight prints the mismatch count and the rows whose weekday may be the
    web builder's 0 = Sunday. Downgrade: `frequency` goes and the dates come back as local midnight;
    the old pair is untouched. A second upgrade lands on the same rows."""
    from alembic import command

    from business_app import db
    from business_app.models.delivery import DeliveryTimeSlot
    from business_app.models.subscription import Subscription
    from business_app.services.subscription_schedule import first_delivery_on_or_after
    from business_app.utils.local_windows import local_date

    app = _migration_app(ephemeral_pg_database)
    with app.app_context():
        config = app.extensions["migrate"].migrate.get_config()
        command.upgrade(config, SUBSCRIPTION_FREQUENCY_PARENT_REVISION)
        w = _old_subscription_world(db)
        capsys.readouterr()

        command.upgrade(config, SUBSCRIPTION_FREQUENCY_REVISION)

        printed = capsys.readouterr().out
        assert "1 subscription(s) have billing_cycle != delivery_frequency" in printed
        assert f"delivery_day_of_week not set by an admin (may be 0 = Sunday): [{w.weekly_thu}]" in printed
        assert _rows(db.engine, _SUBSCRIPTION_STATE) == _expected_upgraded(w)
        with db.engine.connect() as conn:
            columns = _subscription_columns(conn)
        assert columns["frequency"] == ("subscription_frequency", "NO")
        assert columns["next_delivery_date"] == ("date", "YES")
        assert columns["last_delivery_date"] == ("date", "YES")
        # The migration's frozen anchor rule agrees with the live one, row by row. The frozen rule predates delivery
        # days (spec 2026-10-27 §6.4), so they are compared where every day is one: an every-day slot is active.
        db.session.add(DeliveryTimeSlot(name="Every day", start_time="08:00", end_time="20:00", is_active=True,
                                        available_days=list(range(7))))
        db.session.commit()
        billed = dict(_rows(db.engine, "SELECT id, next_billing_date FROM subscriptions"))
        for sub in Subscription.query.order_by(Subscription.id):
            assert first_delivery_on_or_after(sub, local_date(billed[sub.id])) == sub.next_delivery_date
        db.session.remove()

        command.downgrade(config, SUBSCRIPTION_FREQUENCY_PARENT_REVISION)

        with db.engine.connect() as conn:
            columns = _subscription_columns(conn)
        assert "frequency" not in columns
        assert columns["next_delivery_date"][0] == columns["last_delivery_date"][0] == "timestamptz"
        assert _rows(
            db.engine,
            "SELECT billing_cycle::text, delivery_frequency::text, next_delivery_date, last_delivery_date "
            "FROM subscriptions WHERE id IN (:a, :b) ORDER BY id",
            a=w.mismatched,
            b=w.weekly_thu,
        ) == [
            ("monthly", "weekly", datetime(2026, 10, 31, 19, 0, tzinfo=UTC), None),
            ("weekly", "weekly", datetime(2026, 10, 28, 19, 0, tzinfo=UTC), datetime(2026, 10, 19, 19, 0, tzinfo=UTC)),
        ]

        command.upgrade(config, SUBSCRIPTION_FREQUENCY_REVISION)

        assert _rows(db.engine, _SUBSCRIPTION_STATE) == _expected_upgraded(w)


@pytest.mark.integration
def test_subscription_frequency_refuses_a_row_it_cannot_schedule(ephemeral_pg_database):
    """A weekday outside 0-6 has no anchor: the upgrade aborts naming the row and writes nothing."""
    from alembic import command

    from business_app import db

    app = _migration_app(ephemeral_pg_database)
    with app.app_context():
        config = app.extensions["migrate"].migrate.get_config()
        command.upgrade(config, SUBSCRIPTION_FREQUENCY_PARENT_REVISION)
        w = _old_subscription_world(db)
        _run(db.engine, f"UPDATE subscriptions SET delivery_day_of_week = 7 WHERE id = {w.weekly_thu}")

        refusal = rf"no delivery date can be scheduled for subscription\(s\) \[{w.weekly_thu}\]"
        with pytest.raises(RuntimeError, match=refusal):
            command.upgrade(config, SUBSCRIPTION_FREQUENCY_REVISION)

        assert _revision(db.engine) == SUBSCRIPTION_FREQUENCY_PARENT_REVISION
        with db.engine.connect() as conn:
            assert "frequency" not in _subscription_columns(conn)


# ---- Subscriptions: the contract step drops the four old columns (plan 2026-10-24 Task 6) ----

SUBSCRIPTION_CONTRACT_REVISION = "7c3d9e5a1b84"
_OLD_SUBSCRIPTION_COLUMNS = {"billing_cycle", "delivery_frequency", "next_billing_date", "last_billing_date"}
_NEW_SUBSCRIPTION_STATE = (
    "SELECT id, frequency::text, delivery_day_of_week, delivery_day_of_month, next_delivery_date, "
    "last_delivery_date FROM subscriptions ORDER BY id"
)
_BILLING_INDEX = (
    "SELECT count(*) FROM pg_indexes WHERE tablename = 'subscriptions' "
    "AND indexname = 'idx_subscriptions_status_next_billing'"
)


@pytest.mark.integration
def test_subscription_contract_drops_the_old_columns_and_the_downgrade_restores_them(ephemeral_pg_database):
    """Upgrade: billing_cycle, delivery_frequency, next_billing_date, last_billing_date and their index are
    gone; `frequency` and the delivery DATEs are untouched. Downgrade: the four come back NOT NULL (the last
    nullable), the pair copied from `frequency`, next_billing_date the local midnight of next_delivery_date
    (now() when there is none), last_billing_date the local midnight of last_delivery_date; the index returns.
    A second upgrade lands on the same rows."""
    from alembic import command

    from business_app import db

    app = _migration_app(ephemeral_pg_database)
    with app.app_context():
        config = app.extensions["migrate"].migrate.get_config()
        command.upgrade(config, SUBSCRIPTION_FREQUENCY_PARENT_REVISION)
        w = _old_subscription_world(db)
        command.upgrade(config, SUBSCRIPTION_FREQUENCY_REVISION)
        before = _rows(db.engine, _NEW_SUBSCRIPTION_STATE)
        assert before[0][4] == date(2026, 11, 1)
        _run(db.engine, f"UPDATE subscriptions SET next_delivery_date = NULL WHERE id = {w.daily_slot}")
        before = _rows(db.engine, _NEW_SUBSCRIPTION_STATE)

        command.upgrade(config, SUBSCRIPTION_CONTRACT_REVISION)

        with db.engine.connect() as conn:
            columns = _subscription_columns(conn)
        assert _OLD_SUBSCRIPTION_COLUMNS.isdisjoint(columns)
        assert columns["frequency"] == ("subscription_frequency", "NO")
        assert _rows(db.engine, _BILLING_INDEX) == [(0,)]
        assert _rows(db.engine, _NEW_SUBSCRIPTION_STATE) == before

        command.downgrade(config, SUBSCRIPTION_FREQUENCY_REVISION)

        with db.engine.connect() as conn:
            columns = _subscription_columns(conn)
        assert columns["billing_cycle"] == ("subscription_frequency", "NO")
        assert columns["delivery_frequency"] == ("subscription_frequency", "NO")
        assert columns["next_billing_date"] == ("timestamptz", "NO")
        assert columns["last_billing_date"] == ("timestamptz", "YES")
        assert _rows(db.engine, _BILLING_INDEX) == [(1,)]
        assert _rows(db.engine, "SELECT count(*) FROM subscriptions WHERE billing_cycle <> frequency "
                                "OR delivery_frequency <> frequency") == [(0,)]
        assert _rows(
            db.engine,
            "SELECT next_billing_date, last_billing_date FROM subscriptions WHERE id = :a",
            a=w.weekly_thu,
        ) == [(datetime(2026, 10, 28, 19, 0, tzinfo=UTC), datetime(2026, 10, 19, 19, 0, tzinfo=UTC))]
        assert _rows(
            db.engine, "SELECT next_billing_date IS NOT NULL, last_billing_date FROM subscriptions WHERE id = :a",
            a=w.daily_slot,
        ) == [(True, None)]

        command.upgrade(config, SUBSCRIPTION_CONTRACT_REVISION)

        assert _rows(db.engine, _NEW_SUBSCRIPTION_STATE) == before


# ---- Orders: when an order became payable on Click (plan 2026-10-27 Task 3; spec 2026-10-27 §4.4, §17 Q3) ----

ONLINE_PAYABLE_REVISION = "3f8b2d6a9c41"
# Raw SQL, not the ORM: the model already carries `online_payable_since`, which the parent schema lacks.
_ORDER_AT_PARENT = (
    "INSERT INTO orders (order_number, user_id, status, subtotal, total_amount, payment_method, is_paid, "
    "delivery_address_id, created_at, updated_at) VALUES (:number, :user_id, :status, 30000, 30000, :method, "
    ":paid, :address, now() - interval '3 days', now()) RETURNING id"
)


@pytest.mark.integration
def test_online_payable_since_backfills_live_unpaid_online_orders_and_rolls_back(ephemeral_pg_database):
    """At deploy every order still unpaid on an online rail (click, card, payme) and not dead gets the deploy time
    as the start of its payment window, so each has a full 24 hours and nobody is told "not paid by" a deadline
    dated days ago. Cash, cancelled, returned and paid orders stay NULL. The downgrade drops the column; the
    second upgrade lands again."""
    from alembic import command

    from business_app import db
    from business_app.models.user import UserAddress

    app = _migration_app(ephemeral_pg_database)
    with app.app_context():
        config = app.extensions["migrate"].migrate.get_config()
        command.upgrade(config, SUBSCRIPTION_CONTRACT_REVISION)
        user_id = _insert_user(db, phone="+998900000701", password_hash="not-a-real-hash", first_name="Pay")
        # The same table at both revisions, so the ORM is safe; an order past pending needs an address.
        address = UserAddress(user_id=user_id, full_address="Amir Temur 1", city="Tashkent", latitude=41.31,
                              longitude=69.27)
        _add(db, address)
        address_id = _rows(db.engine, "SELECT id FROM addresses")[0][0]

        def order(number, status, method, paid=False):
            params = {"number": f"OPS-{number}", "user_id": user_id, "status": status, "method": method,
                      "paid": paid, "address": address_id}
            with db.engine.begin() as conn:
                return conn.execute(text(_ORDER_AT_PARENT), params).scalar_one()

        stamped = [order(1, "pending", "click"), order(2, "confirmed", "card"), order(3, "delivered", "payme")]
        unstamped = [
            order(4, "pending", "cash"),
            order(5, "cancelled", "click"),
            order(6, "returned", "click"),
            order(7, "pending", "click", paid=True),
        ]
        before = _rows(db.engine, "SELECT now()")[0][0]

        command.upgrade(config, ONLINE_PAYABLE_REVISION)

        starts = dict(_rows(db.engine, "SELECT id, online_payable_since FROM orders"))
        [deploy] = {starts[order_id] for order_id in stamped}
        assert deploy is not None and deploy >= before
        assert [starts[order_id] for order_id in unstamped] == [None] * len(unstamped)

        command.downgrade(config, SUBSCRIPTION_CONTRACT_REVISION)
        with db.engine.connect() as conn:
            assert "online_payable_since" not in _schema_columns(conn, "orders")
        assert _revision(db.engine) == SUBSCRIPTION_CONTRACT_REVISION

        command.upgrade(config, "head")
        with db.engine.connect() as conn:
            assert "online_payable_since" in _schema_columns(conn, "orders")
