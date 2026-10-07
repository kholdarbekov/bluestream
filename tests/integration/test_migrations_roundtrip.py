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
import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import OperationalError


REQUIRES_PG_REASON = (
    "TST-004 migration roundtrip requires PostgreSQL. "
    "Set DATABASE_URL to a Postgres URL with permission to CREATE/DROP databases."
)


def _resolve_database_url() -> str:
    """Pick the most reliable Postgres DSN for the current environment.

    Local dev sometimes has a stale ``DATABASE_URL`` in ``.env`` whose
    password no longer matches the running ``postgres`` container's
    ``POSTGRES_PASSWORD``. When ``POSTGRES_USER``/``PASSWORD``/``DB`` are all
    set, build the DSN from them so we always agree with whatever credentials
    the postgres service actually started with.
    """
    user = os.environ.get('POSTGRES_USER')
    password = os.environ.get('POSTGRES_PASSWORD')
    database = os.environ.get('POSTGRES_DB')
    if user and password and database:
        host = os.environ.get('POSTGRES_HOST', 'postgres')
        port = os.environ.get('POSTGRES_PORT', '5432')
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"
    return os.environ.get('DATABASE_URL', '')


def _admin_engine_for(database_url: str):
    """Engine pointed at the ``postgres`` maintenance DB for CREATE/DROP DATABASE."""
    admin_url = make_url(database_url).set(database='postgres')
    # ``str(URL)`` redacts the password as ``***`` — pass the URL object
    # directly so SQLAlchemy keeps the real credential.
    return create_engine(admin_url, isolation_level='AUTOCOMMIT')


@pytest.fixture
def ephemeral_pg_database():
    """Create a transient Postgres database, yield its URL, drop on teardown."""
    base_url = _resolve_database_url()
    if not base_url.startswith(('postgresql://', 'postgresql+', 'postgres://')):
        pytest.skip(REQUIRES_PG_REASON)

    admin_engine = _admin_engine_for(base_url)
    db_name = f"migr_rt_{uuid.uuid4().hex[:12]}"
    quoted = f'"{db_name}"'

    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE {quoted}'))
    except OperationalError as exc:
        admin_engine.dispose()
        pytest.skip(f"Postgres unreachable for migration roundtrip: {exc.orig}")

    # render_as_string(hide_password=False) keeps the real credential;
    # ``str(URL)`` would redact it to ``***``.
    target_url = make_url(base_url).set(database=db_name).render_as_string(
        hide_password=False
    )
    try:
        yield target_url
    finally:
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname = :db AND pid <> pg_backend_pid()"
                ),
                {'db': db_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS {quoted}'))
        admin_engine.dispose()


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
    from business_app.models.sales_pay import SalesAgentPayTerms, SalesPayPeriod, SalesPayPlan
    from business_app.models.user import User
    from business_app.utils.constants import ORDER_SOURCE_SALES_AGENT
    from tests.integration.sales_pay_builders import make_product
    from tests.integration.test_sales_agent_pg_constraints import _agent_and_outlet, _order

    agent, _outlet = _agent_and_outlet(db, phone="+998900000501", outlet_name="Tier Shop")
    customer = User(phone="+998900000502", password_hash="not-a-real-hash", first_name="Store")
    db.session.add(customer)
    db.session.commit()
    water = make_product(db, name="Water 19L", price="20000")
    plan = SalesPayPlan(name="Standard", created_by_user_id=agent.id)
    period = SalesPayPeriod(month_start=OCT, status="open", is_shadow=False, holidays=[])
    order = _order(db, user_id=customer.id, order_source=ORDER_SOURCE_SALES_AGENT, created_by_staff_id=agent.id)
    db.session.add_all([plan, period, order])
    db.session.commit()
    terms = SalesAgentPayTerms(
        agent_user_id=agent.id,
        effective_month=OCT,
        base_salary=Decimal("2600000"),
        plan_id=plan.id,
        created_by_user_id=agent.id,
    )
    db.session.add(terms)
    db.session.commit()
    w = SimpleNamespace(
        agent=agent.id, water=water.id, plan=plan.id, period=period.id, order=order.id, terms=terms.id
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
    - head → a4c7e9b2d5f8 then passes.
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
            command.upgrade(config, "head")
        assert _revision(db.engine) == PAY_V4_REVISION
        assert _rows(db.engine, "SELECT count(*) FROM sales_pay_plan_rates") == [(2,)]
        with db.engine.connect() as conn:
            assert "from_unit" not in _schema_columns(conn, "sales_pay_plan_rates")

        _run(db.engine, _DELETE_COMMISSION_LINES)
        command.upgrade(config, "head")

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

        command.upgrade(config, "head")
        command.downgrade(config, PAY_PARENT_REVISION)
        _assert_pay_schema(db.engine, present=False)
