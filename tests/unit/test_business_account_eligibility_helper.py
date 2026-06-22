"""Tests for CorporateContractService.get_business_account_balances — the
single source of truth for "is business_account offered to this user".

Four required scenarios:
  1. Individual user → []
  2. Grocery-store entity → [] (even with an active contract)
  3. Workplace entity, no active contract/balance → []
  4. Workplace entity with an active contract → non-empty list
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from business_app import db
from business_app.models.corporate import (
    CorporateContract,
    CorporateContractStatus,
    CorporatePrepaymentAccount,
)
from business_app.models.user import User
from business_app.services.corporate_contract_service import CorporateContractService
from shared.enums import CorporateContractTrackingMode, EntitySubtype, UserRole, UserType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_individual_user() -> User:
    user = User(
        email=f"ind-{uuid4().hex[:8]}@example.com",
        phone=f"+99893{uuid4().int % 10000000:07d}",
        password_hash="x" * 60,
        first_name="Individual",
        last_name="User",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _make_grocery_user() -> User:
    user = User(
        email=f"gs-{uuid4().hex[:8]}@example.com",
        phone=f"+99894{uuid4().int % 10000000:07d}",
        password_hash="x" * 60,
        first_name="Grocery",
        last_name="Store",
        user_type=UserType.ENTITY,
        entity_subtype=EntitySubtype.GROCERY_STORE,
        company_name="Test Grocery Store",
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _make_workplace_user() -> User:
    user = User(
        email=f"wp-{uuid4().hex[:8]}@example.com",
        phone=f"+99895{uuid4().int % 10000000:07d}",
        password_hash="x" * 60,
        first_name="Workplace",
        last_name="Office",
        user_type=UserType.ENTITY,
        entity_subtype=EntitySubtype.WORKPLACE,
        company_name="Test Workplace",
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _make_active_contract(user_id: int) -> CorporateContract:
    """Create a minimal ACTIVE contract with a prepayment account."""
    contract = CorporateContract(
        user_id=user_id,
        contract_number=f"BAE-{uuid4().hex[:10]}",
        name="Eligibility Test Contract",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1),
        currency="UZS",
        is_active=True,
        tracking_mode=CorporateContractTrackingMode.AMOUNT,
    )
    db.session.add(contract)
    db.session.flush()
    db.session.add(CorporatePrepaymentAccount(contract_id=contract.id, is_active=True))
    db.session.commit()
    return contract


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_individual_user_has_no_business_account_balances(db):
    user = _make_individual_user()
    assert CorporateContractService().get_business_account_balances(user) == []


def test_grocery_store_has_no_business_account_balances(db):
    """Grocery stores must never get business_account even with an active contract."""
    grocery = _make_grocery_user()
    _make_active_contract(grocery.id)
    assert CorporateContractService().get_business_account_balances(grocery) == []


def test_workplace_without_active_contract_has_no_balances(db):
    wp = _make_workplace_user()
    # No contract created — no active balances.
    assert CorporateContractService().get_business_account_balances(wp) == []


def test_workplace_with_active_contract_has_balances(db):
    wp = _make_workplace_user()
    _make_active_contract(wp.id)
    result = CorporateContractService().get_business_account_balances(wp)
    assert len(result) >= 1
    # Returned list items should have the expected contract key.
    assert "contract" in result[0]


def test_none_user_returns_empty_list(db):
    """None guard: should never raise."""
    assert CorporateContractService().get_business_account_balances(None) == []
