"""CustomerCreditPayout / CustomerCreditPayoutDebit schema rules (SQLite enforces CHECKs; FKs are off)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from business_app.models.payment import CustomerCreditPayout, CustomerCreditPayoutDebit

NOW = datetime(2026, 11, 10, 7, 0, tzinfo=UTC)


def _payout(user_id, **overrides):
    values = {
        "customer_id": user_id,
        "amount": Decimal("30000.00"),
        "paid_by": "office",
        "reason": "paid back in cash",
        "paid_at": NOW,
        "actor_user_id": user_id,
        "idempotency_key": f"key-{overrides.pop('key', 'a')}",
    }
    values.update(overrides)
    return CustomerCreditPayout(**values)


def _refused(db, *rows):
    db.session.add_all(rows)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_a_valid_office_payout_commits(db, sample_user):
    db.session.add(_payout(sample_user.id))
    db.session.commit()
    assert CustomerCreditPayout.query.one().paid_by == "office"


def test_paid_by_outside_vocabulary_is_refused(db, sample_user):
    _refused(db, _payout(sample_user.id, paid_by="bank"))


@pytest.mark.parametrize(
    "paid_by,driver,session",
    [
        ("office", 7, None),
        ("office", None, 3),
        ("driver_settled", None, None),
        ("driver_settled", 7, 3),
        ("driver_current_session", 7, None),
        ("driver_current_session", None, 3),
    ],
)
def test_payer_shape_is_enforced(db, sample_user, paid_by, driver, session):
    _refused(
        db,
        _payout(sample_user.id, paid_by=paid_by, driver_user_id=driver, driver_cash_session_id=session),
    )


@pytest.mark.parametrize(
    "paid_by,driver,session",
    [("driver_settled", 7, None), ("driver_current_session", 7, 3)],
)
def test_valid_driver_shapes_commit(db, sample_user, paid_by, driver, session):
    db.session.add(_payout(sample_user.id, paid_by=paid_by, driver_user_id=driver, driver_cash_session_id=session))
    db.session.commit()
    assert CustomerCreditPayout.query.one().paid_by == paid_by


def test_amounts_must_be_positive(db, sample_user):
    _refused(db, _payout(sample_user.id, amount=Decimal("0.00")))
    payout = _payout(sample_user.id)
    db.session.add(payout)
    db.session.commit()
    _refused(
        db,
        CustomerCreditPayoutDebit(
            payout_id=payout.id,
            cash_collection_event_id=1,
            amount=Decimal("0.00"),
            revenue_counted=False,
            card_click=False,
        ),
    )


def test_duplicate_debit_per_payout_and_event_is_refused(db, sample_user):
    payout = _payout(sample_user.id)
    db.session.add(payout)
    db.session.commit()
    fields = dict(payout_id=payout.id, cash_collection_event_id=5, amount=Decimal("1.00"),
                  revenue_counted=False, card_click=False)
    db.session.add(CustomerCreditPayoutDebit(**fields))
    db.session.commit()
    _refused(db, CustomerCreditPayoutDebit(**fields))


def test_duplicate_idempotency_key_is_refused(db, sample_user):
    db.session.add(_payout(sample_user.id, key="same"))
    db.session.commit()
    _refused(db, _payout(sample_user.id, key="same"))


def test_void_needs_a_reason(db, sample_user):
    _refused(db, _payout(sample_user.id, voided_at=NOW, voided_by_user_id=sample_user.id, void_reason=None))
    _refused(db, _payout(sample_user.id, key="b", void_reason="why"))


def test_is_live_agrees_in_python_and_sql(db, sample_user):
    live = _payout(sample_user.id, key="live")
    undone = _payout(
        sample_user.id, key="undone", voided_at=NOW, voided_by_user_id=sample_user.id, void_reason="entered twice"
    )
    db.session.add_all([live, undone])
    db.session.commit()
    assert [p.is_live for p in (live, undone)] == [True, False]
    assert CustomerCreditPayout.query.filter(CustomerCreditPayout.is_live).all() == [live]
