"""Postgres-only guarantees of migration 3d8f5a1c7e92 (driver cash expenses).

Runs the real migration on a throwaway database and asserts each constraint BY NAME. Each probe row breaks
exactly ONE check: Postgres tests CHECKs in alphabetical order of name and reports the first that fails, so a
pending row with `withdrawn_at` would be reported as `pending_clean`, not `withdrawn`.
"""

import itertools
from datetime import UTC, datetime
from decimal import Decimal
from importlib import import_module

import pytest
from sqlalchemy import CheckConstraint, text
from sqlalchemy.exc import IntegrityError

from business_app.models.driver_expense import DriverCashExpense
from business_app.models.payment import DriverCashSession
from business_app.models.user import User
from shared.staff_constants import (
    DRIVER_EXPENSE_CATEGORIES,
    DRIVER_EXPENSE_NOTE_REQUIRED_CATEGORIES,
    DRIVER_EXPENSE_STATUSES,
)
from tests.integration.test_sales_agent_pg_constraints import (
    _constraint_def,
    _rollback_on_named_integrity,
)

pytestmark = pytest.mark.integration

TABLE = "driver_cash_expenses"
NOW = datetime(2026, 11, 18, 7, 0, tzinfo=UTC)
_phones = itertools.count(900000701)
_keys = itertools.count(1)


def _load_migration():
    return import_module("business_app.migrations.versions.20261118_1200_driver_cash_expenses")


def _user(pg_db):
    user = User(phone=f"+998{next(_phones)}", password_hash="not-a-real-hash", first_name="Davron")
    pg_db.session.add(user)
    pg_db.session.commit()
    return user


def _session(pg_db, user):
    row = DriverCashSession(session_id=f"pg-session-{next(_keys)}", driver_user_id=user.id)
    pg_db.session.add(row)
    pg_db.session.commit()
    return row


def _expense(user, session, **overrides):
    values = {
        "driver_user_id": user.id,
        "driver_cash_session_id": session.id,
        "category": "fuel",
        "amount": Decimal("150000.00"),
        "status": "pending",
        "idempotency_key": f"pg-key-{next(_keys)}",
        "self_decided": False,
        "submitted_at": NOW,
    }
    values.update(overrides)
    return DriverCashExpense(**values)


def test_the_migrations_frozen_tuples_match_the_shared_ones():
    migration = _load_migration()
    assert migration._CATEGORIES == DRIVER_EXPENSE_CATEGORIES
    assert migration._NOTE_REQUIRED == DRIVER_EXPENSE_NOTE_REQUIRED_CATEGORIES
    assert migration._STATUSES == DRIVER_EXPENSE_STATUSES


def test_the_migration_chains_on_the_previous_head():
    assert _load_migration().down_revision == "7b3f1c9e4a62"


def test_the_models_check_text_is_the_migrations_check_text(pg_db):
    """The CHECK text lives twice (the model, the frozen migration). Render the model's text in Postgres on a
    column-for-column copy of the migrated table and compare it with what the migration created."""
    checks = [c for c in DriverCashExpense.__table__.constraints if isinstance(c, CheckConstraint)]
    migrated = {
        name
        for (name,) in pg_db.session.execute(
            text("SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(:t)::oid AND contype = 'c'"),
            {"t": TABLE},
        )
    }
    assert {c.name for c in checks} == migrated and len(checks) == 9
    scratch = f"parity_{TABLE}"
    connection = pg_db.session.connection()
    connection.exec_driver_sql(f"CREATE TEMP TABLE {scratch} (LIKE {TABLE})")
    for check in checks:
        connection.exec_driver_sql(f"ALTER TABLE {scratch} ADD CONSTRAINT {check.name} CHECK ({check.sqltext})")
        assert _constraint_def(pg_db, scratch, check.name) == _constraint_def(pg_db, TABLE, check.name), check.name
    pg_db.session.rollback()


def test_the_unique_key_and_the_three_indexes_exist(pg_db):
    indexes = dict(
        pg_db.session.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t"), {"t": TABLE}
        ).all()
    )
    assert "(driver_user_id, idempotency_key)" in indexes["uq_driver_cash_expenses_driver_idempotency_key"]
    assert "(status, submitted_at)" in indexes["ix_driver_cash_expenses_status_submitted"]
    assert "(driver_cash_session_id, status)" in indexes["ix_driver_cash_expenses_session_status"]
    assert "(driver_user_id, status)" in indexes["ix_driver_cash_expenses_driver_status"]


APPROVED = {"status": "approved", "decided_by_user_id": "U", "decided_at": NOW}


@pytest.mark.parametrize(
    "overrides, constraint",
    [
        ({"amount": Decimal("0")}, "ck_driver_cash_expenses_amount_positive"),
        ({"category": "bottles"}, "ck_driver_cash_expenses_category"),
        ({"category": "other", "note": "  "}, "ck_driver_cash_expenses_note_required"),
        ({"status": "lost"}, "ck_driver_cash_expenses_status"),
        (dict(APPROVED), "ck_driver_cash_expenses_approved_amount"),
        (dict(APPROVED, approved_amount=Decimal("150000.01")), "ck_driver_cash_expenses_approved_amount"),
        (
            {"status": "rejected", "decided_by_user_id": "U", "decided_at": NOW},
            "ck_driver_cash_expenses_rejected_reason",
        ),
        ({"status": "withdrawn"}, "ck_driver_cash_expenses_withdrawn"),
        (
            dict(APPROVED, approved_amount=Decimal("150000"), withdrawn_at=NOW),
            "ck_driver_cash_expenses_withdrawn",
        ),
        ({"status": "rejected", "decision_reason": "Not ours", "decided_at": NOW}, "ck_driver_cash_expenses_decided"),
        ({"decision_reason": "Not ours"}, "ck_driver_cash_expenses_pending_clean"),
        ({"self_decided": True}, "ck_driver_cash_expenses_pending_clean"),
    ],
)
def test_each_invalid_shape_is_refused_by_its_named_constraint(pg_db, overrides, constraint):
    user = _user(pg_db)
    session = _session(pg_db, user)
    overrides = {k: (user.id if v == "U" else v) for k, v in overrides.items()}
    _rollback_on_named_integrity(pg_db, _expense(user, session, **overrides), constraint)


def test_the_valid_shapes_are_accepted(pg_db):
    user = _user(pg_db)
    session = _session(pg_db, user)
    pg_db.session.add_all(
        [
            _expense(user, session),
            _expense(user, session, category="other", note="tyre valve"),
            _expense(user, session, **dict(APPROVED, decided_by_user_id=user.id, approved_amount=Decimal("100000"))),
            _expense(
                user, session, status="rejected", decided_by_user_id=user.id, decided_at=NOW,
                decision_reason="Not ours",
            ),
            _expense(user, session, status="withdrawn", withdrawn_at=NOW),
        ]
    )
    pg_db.session.commit()
    assert pg_db.session.query(DriverCashExpense).count() == 5


def test_a_key_is_unique_per_driver_only(pg_db):
    first, second = _user(pg_db), _user(pg_db)
    session_one, session_two = _session(pg_db, first), _session(pg_db, second)
    pg_db.session.add(_expense(first, session_one, idempotency_key="dup"))
    pg_db.session.commit()
    _rollback_on_named_integrity(
        pg_db, _expense(first, session_one, idempotency_key="dup"), "uq_driver_cash_expenses_driver_idempotency_key"
    )
    pg_db.session.add(_expense(second, session_two, idempotency_key="dup"))
    pg_db.session.commit()  # another driver may reuse the key


def test_the_driver_and_session_are_required(pg_db):
    user = _user(pg_db)
    row = _expense(user, _session(pg_db, user))
    row.driver_cash_session_id = None
    pg_db.session.add(row)
    with pytest.raises(IntegrityError):
        pg_db.session.commit()
    pg_db.session.rollback()
