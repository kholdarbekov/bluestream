"""One rule for "this account has a corporate contract in force", for one account and for many.

The single answer (`has_active_contract`) and the bulk answer (`user_ids_with_active_contract`)
must agree for every account, and both must agree with the contract list the pricing path reads.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from business_app.models.corporate import CorporateContractStatus
from business_app.models.user import User
from business_app.services.corporate_contract_service import CorporateContractService
from shared.enums import CorporateContractTrackingMode, EntitySubtype, UserRole, UserType
from tests.integration.contract_pricing_factory import make_contract, make_workplace

pytestmark = pytest.mark.integration


@pytest.fixture
def svc():
    return CorporateContractService()


def _individual(db):
    user = User(
        email=f"ind-{uuid4().hex[:8]}@example.com",
        phone=f"+99894{uuid4().int % 10000000:07d}",
        password_hash="x" * 60,
        first_name="Plain",
        last_name="Person",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def test_a_workplace_with_a_contract_in_force_has_one(db, svc, sample_product):
    user, _ = make_workplace(db)
    make_contract(db, user, sample_product)

    assert svc.has_active_contract(user.id) is True


def test_an_individual_never_has_one(db, svc, sample_product):
    user = _individual(db)
    make_contract(db, user, sample_product)  # a contract row on a non-entity account

    assert svc.has_active_contract(user.id) is False
    assert svc.user_ids_with_active_contract([user.id]) == set()


@pytest.mark.parametrize(
    "break_it",
    [
        lambda c, now: setattr(c, "start_date", now + timedelta(days=1)),
        lambda c, now: setattr(c, "end_date", now - timedelta(hours=1)),
        lambda c, now: setattr(c, "status", CorporateContractStatus.SUSPENDED),
        lambda c, now: setattr(c, "is_active", False),
    ],
    ids=["starts-tomorrow", "ended-an-hour-ago", "suspended", "inactive"],
)
def test_a_contract_out_of_force_does_not_count(db, svc, sample_product, break_it):
    user, _ = make_workplace(db)
    contract, _row = make_contract(db, user, sample_product)
    now = datetime.now(UTC)
    break_it(contract, now)
    db.session.commit()

    assert svc.has_active_contract(user.id, effective_at=now) is False
    assert svc.user_ids_with_active_contract(effective_at=now) == set()


def test_the_answer_follows_the_instant_asked_about(db, svc, sample_product):
    user, _ = make_workplace(db)
    contract, _row = make_contract(db, user, sample_product)
    now = datetime.now(UTC)
    contract.start_date = now - timedelta(days=10)
    contract.end_date = now - timedelta(days=2)
    db.session.commit()

    assert svc.has_active_contract(user.id, effective_at=now - timedelta(days=5)) is True
    assert svc.has_active_contract(user.id, effective_at=now) is False
    assert svc.has_active_contract(user.id, effective_at=now - timedelta(days=11)) is False
    assert svc.user_ids_with_active_contract(effective_at=now - timedelta(days=5)) == {user.id}
    assert svc.user_ids_with_active_contract(effective_at=now) == set()


@pytest.fixture
def accounts(db, sample_product):
    now = datetime.now(UTC)
    wp_in_force, _ = make_workplace(db)
    make_contract(db, wp_in_force, sample_product)

    wp_ended, _ = make_workplace(db)
    ended, _row = make_contract(db, wp_ended, sample_product)
    ended.end_date = now - timedelta(hours=1)

    grocery, _ = make_workplace(db, subtype=EntitySubtype.GROCERY_STORE)
    make_contract(db, grocery, sample_product, tracking_mode=CorporateContractTrackingMode.AMOUNT)

    individual = _individual(db)
    db.session.commit()
    return now, wp_in_force, wp_ended, grocery, individual


def test_the_bulk_answer_equals_the_single_answer_for_every_account(db, svc, accounts):
    now, wp_in_force, wp_ended, grocery, individual = accounts
    users = [wp_in_force, wp_ended, grocery, individual]

    bulk = svc.user_ids_with_active_contract(effective_at=now)

    assert bulk == {u.id for u in users if svc.has_active_contract(u.id, effective_at=now)}
    assert bulk == {wp_in_force.id, grocery.id}


def test_the_bulk_answer_narrowed_to_given_ids(db, svc, accounts):
    now, _wp_in_force, _wp_ended, grocery, individual = accounts

    assert svc.user_ids_with_active_contract([grocery.id, individual.id], effective_at=now) == {grocery.id}
    assert svc.user_ids_with_active_contract([], effective_at=now) == set()
