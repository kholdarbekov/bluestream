"""Tests for GET /api/v1/payments/methods — business_account visibility.

Four required scenarios:
  1. Workplace entity WITH an active contract balance → method IS present
  2. Individual user → method NOT present
  3. Grocery-store entity WITH an active contract → method NOT present
  4. Workplace entity WITHOUT an active contract/balance → method NOT present
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from business_app import db
from business_app.models.corporate import (
    CorporateContract,
    CorporateContractStatus,
    CorporatePrepaymentAccount,
)
from business_app.models.user import User
from flask_jwt_extended import create_access_token
from shared.enums import CorporateContractTrackingMode, EntitySubtype, UserRole, UserType


# ---------------------------------------------------------------------------
# Local helpers — mirrors helpers from test_business_account_eligibility_helper.py
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


def _make_workplace_or_grocery_user(subtype: EntitySubtype) -> User:
    prefix = "wp" if subtype == EntitySubtype.WORKPLACE else "gs"
    phone_prefix = "95" if subtype == EntitySubtype.WORKPLACE else "94"
    user = User(
        email=f"{prefix}-{uuid4().hex[:8]}@example.com",
        phone=f"+99899{uuid4().int % 10000000:07d}",
        password_hash="x" * 60,
        first_name="Entity",
        last_name="User",
        user_type=UserType.ENTITY,
        entity_subtype=subtype,
        company_name=f"Test {subtype.value}",
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
        name="Payment Methods Test Contract",
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
# Helper: call the endpoint and return the set of method names
# ---------------------------------------------------------------------------


def _methods(client, app, user):
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    resp = client.get(
        "/api/v1/payments/methods",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.get_json()["data"]["available_methods"]
    return {m["method"] for m in data}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_eligible_workplace_sees_business_account(client, app, db):
    wp = _make_workplace_or_grocery_user(EntitySubtype.WORKPLACE)
    _make_active_contract(wp.id)
    assert "business_account" in _methods(client, app, wp)


def test_individual_does_not_see_business_account(client, app, db):
    user = _make_individual_user()
    assert "business_account" not in _methods(client, app, user)


def test_grocery_does_not_see_business_account(client, app, db):
    grocery = _make_workplace_or_grocery_user(EntitySubtype.GROCERY_STORE)
    _make_active_contract(grocery.id)
    assert "business_account" not in _methods(client, app, grocery)


def test_workplace_without_balance_does_not_see_business_account(client, app, db):
    wp = _make_workplace_or_grocery_user(EntitySubtype.WORKPLACE)
    # No contract — no active balances.
    assert "business_account" not in _methods(client, app, wp)


def test_business_account_entry_is_flagged_default(client, app, db):
    wp = _make_workplace_or_grocery_user(EntitySubtype.WORKPLACE)
    _make_active_contract(wp.id)
    with app.app_context():
        token = create_access_token(identity=str(wp.id))
    resp = client.get(
        "/api/v1/payments/methods",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    methods = resp.get_json()["data"]["available_methods"]
    ba = [m for m in methods if m["method"] == "business_account"]
    assert len(ba) == 1
    assert ba[0].get("is_default") is True
    # No non-business_account entry claims the default.
    assert all(not m.get("is_default") for m in methods if m["method"] != "business_account")
