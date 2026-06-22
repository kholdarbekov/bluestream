"""Unit tests for LoyaltyService.is_user_loyalty_eligible SSOT rule.

Six scenarios:
  1. Individual user is always eligible.
  2. Entity user with no contracts is not eligible.
  3. Entity user whose only contract has is_loyalty_points_eligible=False is not eligible.
  4. Entity user whose only contract is SUSPENDED is not eligible.
  5. Entity user whose only contract is expired (end_date in the past) is not eligible.
  6. Entity user with one bad contract + one active-eligible contract IS eligible.
"""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from business_app import db as _db
from business_app.models.corporate import CorporateContract, CorporateContractStatus
from business_app.models.user import User
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserType


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------

def _make_user(db, user_type: UserType) -> User:
    """Create a persisted user with a unique email/phone for this test run."""
    uid = uuid4().hex[:8]
    user = User(
        email=f"test-{uid}@example.com",
        phone=f"+9989{uid[:8]}",
        password_hash=hash_password("TestPassword123!"),
        first_name="Test",
        last_name="User",
        user_type=user_type,
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.utcnow(),
    )
    db.session.add(user)
    db.session.commit()
    return user


def _contract(user, **kw):
    defaults = dict(
        user_id=user.id,
        contract_number=f"C-{user.id}-{kw.get('tag', uuid4().hex[:6])}",
        name="c",
        status=CorporateContractStatus.ACTIVE,
        is_active=True,
        is_loyalty_points_eligible=True,
        start_date=datetime.utcnow() - timedelta(days=1),
        end_date=None,
    )
    defaults.update({k: v for k, v in kw.items() if k != "tag"})
    return CorporateContract(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_individual_user_is_eligible(db):
    user = _make_user(db, UserType.INDIVIDUAL)
    assert LoyaltyService.is_user_loyalty_eligible(user) is True


def test_entity_with_no_contract_is_not_eligible(db):
    user = _make_user(db, UserType.ENTITY)
    assert LoyaltyService.is_user_loyalty_eligible(user) is False


def test_entity_with_disabled_flag_is_not_eligible(db):
    user = _make_user(db, UserType.ENTITY)
    db.session.add(_contract(user, is_loyalty_points_eligible=False))
    db.session.commit()
    assert LoyaltyService.is_user_loyalty_eligible(user) is False


def test_entity_with_suspended_contract_is_not_eligible(db):
    user = _make_user(db, UserType.ENTITY)
    db.session.add(_contract(user, status=CorporateContractStatus.SUSPENDED))
    db.session.commit()
    assert LoyaltyService.is_user_loyalty_eligible(user) is False


def test_entity_with_expired_contract_is_not_eligible(db):
    user = _make_user(db, UserType.ENTITY)
    db.session.add(_contract(user, end_date=datetime.utcnow() - timedelta(days=1)))
    db.session.commit()
    assert LoyaltyService.is_user_loyalty_eligible(user) is False


def test_entity_with_one_active_eligible_contract_is_eligible(db):
    user = _make_user(db, UserType.ENTITY)
    db.session.add(_contract(user, tag="bad", is_loyalty_points_eligible=False))
    db.session.add(_contract(user, tag="good", is_loyalty_points_eligible=True))
    db.session.commit()
    assert LoyaltyService.is_user_loyalty_eligible(user) is True


def test_profile_data_includes_loyalty_eligible_flag(db):
    from business_app.services.auth_service import AuthService

    entity = _make_user(db, UserType.ENTITY)  # no contract -> not eligible
    data = AuthService().get_user_profile_data(entity.id)
    assert data["loyalty_eligible"] is False

    individual = _make_user(db, UserType.INDIVIDUAL)
    data_ind = AuthService().get_user_profile_data(individual.id)
    assert data_ind["loyalty_eligible"] is True
