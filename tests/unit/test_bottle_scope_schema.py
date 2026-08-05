# tests/unit/test_bottle_scope_schema.py
import pytest
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from business_app import db
from business_app.models.bottle import BottleBalance, BottleFine, BottleLedger


def test_balance_has_no_user_id():
    assert not hasattr(BottleBalance, "user_id")


def test_balance_scope_columns_exist():
    cols = BottleBalance.__table__.c
    assert "address_group_id" in cols
    assert cols["address_id"].nullable is True
    assert cols["address_group_id"].nullable is True


def test_balance_scope_uniques_are_plain():
    """Plain UNIQUE, not partial: the CHECK already makes the two mutually
    exclusive, and both Postgres and SQLite treat NULLs as distinct. Partial
    indexes would need postgresql_where AND sqlite_where mirrored, the trap that
    leaves DriverSessionMembership's unique unenforced in the whole suite."""
    names = {c.name for c in BottleBalance.__table__.constraints if c.name}
    assert "uq_bottle_balance_group" in names
    assert "uq_bottle_balance_addr" in names
    assert "uq_bottle_balance_user_address" not in names


def test_balance_has_scope_check_constraint():
    names = {c.name for c in BottleBalance.__table__.constraints if c.name}
    assert "ck_bottle_balance_scope" in names


@pytest.mark.parametrize("kwargs", [{}, {"address_group_id": 1, "address_id": 1}])
def test_balance_scope_check_is_enforced(app, db, user_address, kwargs):
    """The constraint must REJECT rows, not merely exist under the right name.

    Asserting only on the constraint's name passes even if the expression is
    `1=1`. Exactly-one-scope-key is what every `BottleScope.balance_filter()`
    caller rests on, so the two violating shapes — neither key set, and both
    keys set — are pinned here against the backend the suite actually runs on.
    """
    db.session.add(BottleBalance(balance=Decimal("1.00"), **kwargs))
    with pytest.raises(IntegrityError):
        db.session.flush()


def test_ledger_has_scope_column_and_keeps_attribution():
    cols = BottleLedger.__table__.c
    assert "address_group_id" in cols
    assert cols["address_group_id"].nullable is True
    # attribution survives, NOT NULL, per spec section 4.2
    assert cols["user_id"].nullable is False
    assert cols["address_id"].nullable is False


def test_fine_is_address_keyed_not_balance_keyed():
    cols = BottleFine.__table__.c
    assert "address_id" in cols and cols["address_id"].nullable is False
    assert "address_group_id" in cols and cols["address_group_id"].nullable is True
    assert "bottle_balance_id" not in cols


def test_two_ungrouped_balances_on_one_address_rejected(app, db, user_address):
    db.session.add(BottleBalance(address_id=user_address.id, balance=Decimal("1.00")))
    db.session.flush()
    db.session.add(BottleBalance(address_id=user_address.id, balance=Decimal("2.00")))
    with pytest.raises(Exception):
        db.session.flush()
