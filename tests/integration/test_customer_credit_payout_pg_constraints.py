"""Postgres-only guarantees of migrations 5e2b9c71d4a8 and c8a4f2d6b1e3 (customer credit payouts).

Runs the real migration on a throwaway database and asserts each constraint BY NAME, so a probe
that breaks two rules cannot pass against a schema missing the one under test.
"""

import itertools
from datetime import UTC, datetime
from decimal import Decimal
from importlib import import_module

import pytest
from sqlalchemy import CheckConstraint, text

from business_app.models.payment import (
    CashCollectionEvent,
    CustomerCreditPayout,
    CustomerCreditPayoutDebit,
    DriverCashSession,
)
from business_app.models.user import User
from shared.enums import CashCollectionSource
from tests.integration.test_sales_agent_pg_constraints import (
    _constraint_def,
    _quoted_literals,
    _rollback_on_named_integrity,
)

pytestmark = pytest.mark.integration

PAYOUTS = "customer_credit_payouts"
DEBITS = "customer_credit_payout_debits"
NOW = datetime(2026, 11, 10, 7, 0, tzinfo=UTC)
_phones = itertools.count(900000601)
_keys = itertools.count(1)


def _user(pg_db):
    user = User(phone=f"+998{next(_phones)}", password_hash="not-a-real-hash", first_name="Kamola")
    pg_db.session.add(user)
    pg_db.session.commit()
    return user


def _session(pg_db, user):
    row = DriverCashSession(session_id=f"pg-session-{next(_keys)}", driver_user_id=user.id)
    pg_db.session.add(row)
    pg_db.session.commit()
    return row


def _payout(user, **overrides):
    values = {
        "customer_id": user.id,
        "amount": Decimal("30000.00"),
        "paid_by": "office",
        "reason": "paid back in cash",
        "paid_at": NOW,
        "actor_user_id": user.id,
        "idempotency_key": f"pg-key-{next(_keys)}",
    }
    values.update(overrides)
    return CustomerCreditPayout(**values)


def test_the_migrations_frozen_paid_by_matches_the_model(pg_db):
    migration = import_module("business_app.migrations.versions.20261110_1200_add_customer_credit_payouts")
    assert migration.PAID_BY == CustomerCreditPayout.PAID_BY_VALUES
    definition = _constraint_def(pg_db, PAYOUTS, "ck_customer_credit_payouts_paid_by")
    assert definition is not None
    assert _quoted_literals(definition) == set(CustomerCreditPayout.PAID_BY_VALUES)


@pytest.mark.parametrize("model", [CustomerCreditPayout, CustomerCreditPayoutDebit])
def test_the_models_check_text_is_the_migrations_check_text(pg_db, model):
    """The CHECK text lives twice (the model, the frozen migration). Render the model's text in Postgres on a
    column-for-column copy of the migrated table and compare it with what the migration created: any drift in a
    condition, a literal or a name fails here, not in production."""
    table = model.__table__.name
    checks = [c for c in model.__table__.constraints if isinstance(c, CheckConstraint)]
    migrated = {
        name
        for (name,) in pg_db.session.execute(
            text("SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(:t)::oid AND contype = 'c'"),
            {"t": table},
        )
    }
    assert {c.name for c in checks} == migrated and checks
    scratch = f"parity_{table}"
    connection = pg_db.session.connection()
    connection.exec_driver_sql(f"CREATE TEMP TABLE {scratch} (LIKE {table})")
    for check in checks:
        connection.exec_driver_sql(f"ALTER TABLE {scratch} ADD CONSTRAINT {check.name} CHECK ({check.sqltext})")
        assert _constraint_def(pg_db, scratch, check.name) == _constraint_def(pg_db, table, check.name), check.name
    pg_db.session.rollback()


@pytest.mark.parametrize(
    "overrides, constraint",
    [
        ({"amount": Decimal("0.00")}, "ck_customer_credit_payouts_amount_positive"),
        ({"paid_by": "bank"}, "ck_customer_credit_payouts_paid_by"),
        ({"paid_by": "office", "driver_user_id": "driver"}, "ck_customer_credit_payouts_payer_shape"),
        ({"paid_by": "driver_settled"}, "ck_customer_credit_payouts_payer_shape"),
        ({"paid_by": "driver_settled", "driver_user_id": "driver", "driver_cash_session_id": 3},
         "ck_customer_credit_payouts_payer_shape"),
        ({"paid_by": "driver_current_session", "driver_user_id": "driver"}, "ck_customer_credit_payouts_payer_shape"),
        ({"voided_at": NOW}, "ck_customer_credit_payouts_void_shape"),
        ({"void_reason": "why"}, "ck_customer_credit_payouts_void_shape"),
        ({"target_balance": Decimal("-0.01")}, "ck_customer_credit_payouts_target_balance_nonneg"),
    ],
)
def test_each_invalid_shape_is_refused_by_its_named_constraint(pg_db, overrides, constraint):
    user = _user(pg_db)
    overrides = {k: (user.id if v == "driver" else v) for k, v in overrides.items()}
    if overrides.get("driver_cash_session_id") is not None:
        overrides["driver_cash_session_id"] = _session(pg_db, user).id  # a real session: only the CHECK may refuse
    _rollback_on_named_integrity(pg_db, _payout(user, **overrides), constraint)


def test_duplicate_idempotency_key_is_refused(pg_db):
    user = _user(pg_db)
    pg_db.session.add(_payout(user, idempotency_key="dup"))
    pg_db.session.commit()
    _rollback_on_named_integrity(
        pg_db, _payout(user, idempotency_key="dup"), "uq_customer_credit_payouts_idempotency_key"
    )


def test_debit_rules_are_enforced_by_name(pg_db):
    user = _user(pg_db)
    payout = _payout(user)
    event = CashCollectionEvent(
        customer_id=user.id, amount=Decimal("50000.00"), currency="UZS",
        source=CashCollectionSource.ADMIN_ADJUSTMENT, occurred_at=NOW, unapplied_amount=Decimal("50000.00"),
    )
    pg_db.session.add_all([payout, event])
    pg_db.session.commit()
    fields = dict(payout_id=payout.id, cash_collection_event_id=event.id, revenue_counted=False, card_click=False)
    _rollback_on_named_integrity(
        pg_db, CustomerCreditPayoutDebit(amount=Decimal("0.00"), **fields), "ck_customer_credit_payout_debits_amount_positive"
    )
    pg_db.session.add(CustomerCreditPayoutDebit(amount=Decimal("1.00"), **fields))
    pg_db.session.commit()
    _rollback_on_named_integrity(
        pg_db, CustomerCreditPayoutDebit(amount=Decimal("2.00"), **fields), "uq_customer_credit_payout_debits_payout_event"
    )


def test_the_customer_and_driver_user_fks_restrict_deletion(pg_db):
    rows = pg_db.session.execute(
        text(
            "SELECT conname, confdeltype FROM pg_constraint "
            "WHERE conrelid = to_regclass(:t)::oid AND contype = 'f'"
        ),
        {"t": PAYOUTS},
    ).all()
    delete_rules = dict(rows)
    assert delete_rules["fk_customer_credit_payouts_customer_id"] == "r"
    assert delete_rules["fk_customer_credit_payouts_driver_user_id"] == "r"
