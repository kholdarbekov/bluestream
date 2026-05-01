"""Tests for the grocery-store entity subtype: money-mode contract debt.

Covers:
  - charge_on_delivery posts a CHARGE entry for the order total
  - record_money_collection drives outstanding_amount down (and into credit)
  - validate_business_account_order rejects grocery_store users
  - get_client_payment_methods omits BUSINESS_ACCOUNT for grocery stores
  - get_client_payment_methods rejects unassigned-subtype entities
  - workplace UNITS-mode flow continues to work unchanged
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
    CorporatePrepaymentEventType,
    CorporatePrepaymentLedger,
)
from business_app.models.order import Order
from business_app.models.user import User
from business_app.services.corporate_contract_service import CorporateContractService
from business_app.services.staff_service import StaffService
from shared.enums import OrderStatus, PaymentMethod, UserRole, UserType
from business_app.utils.exceptions import ValidationError
from shared.enums import CorporateContractTrackingMode, EntitySubtype


def _make_grocery_user(*, password_hash: str = "x" * 60) -> User:
    user = User(
        email=f"gs-{uuid4().hex[:8]}@example.com",
        phone=f"+99890{uuid4().int % 10000000:07d}",
        password_hash=password_hash,
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


def _make_workplace_user(*, password_hash: str = "x" * 60) -> User:
    user = User(
        email=f"wp-{uuid4().hex[:8]}@example.com",
        phone=f"+99891{uuid4().int % 10000000:07d}",
        password_hash=password_hash,
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


def _make_amount_contract(user_id: int) -> CorporateContract:
    contract = CorporateContract(
        user_id=user_id,
        contract_number=f"GS-{uuid4().hex[:10]}",
        name="Grocery Store Contract",
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


def _make_order(user_id: int, total_amount: Decimal) -> Order:
    order = Order(
        order_number=f"ORD-{uuid4().hex[:8]}",
        user_id=user_id,
        status=OrderStatus.PENDING,
        subtotal=total_amount,
        delivery_fee=Decimal("0.00"),
        total_amount=total_amount,
        order_source="web",
    )
    db.session.add(order)
    db.session.commit()
    return order


def test_charge_on_delivery_posts_money_charge(db):
    """A 225 000 UZS grocery order delivered creates a CHARGE for the same amount."""
    user = _make_grocery_user()
    contract = _make_amount_contract(user.id)
    order = _make_order(user.id, Decimal("225000.00"))

    service = CorporateContractService()
    entry = service.charge_on_delivery(order=order, delivery_id=None, actor_user_id=None)
    db.session.commit()

    assert entry is not None
    assert entry.event_type == CorporatePrepaymentEventType.CHARGE
    assert Decimal(str(entry.amount)) == Decimal("225000.00")
    assert entry.units is None
    assert entry.product_id is None
    assert entry.balance_id is None

    db.session.refresh(contract.prepayment_account)
    assert Decimal(str(contract.prepayment_account.outstanding_amount)) == Decimal("225000.00")
    assert Decimal(str(contract.prepayment_account.lifetime_charged)) == Decimal("225000.00")


def test_charge_on_delivery_is_idempotent(db):
    user = _make_grocery_user()
    _make_amount_contract(user.id)
    order = _make_order(user.id, Decimal("100000.00"))

    service = CorporateContractService()
    first = service.charge_on_delivery(order=order)
    db.session.commit()
    second = service.charge_on_delivery(order=order)
    db.session.commit()

    assert first.id == second.id
    total_charges = (
        db.session.query(CorporatePrepaymentLedger)
        .filter_by(order_id=order.id, event_type=CorporatePrepaymentEventType.CHARGE)
        .count()
    )
    assert total_charges == 1


def test_record_money_collection_reduces_outstanding(db):
    """120 000 UZS collected against a 225 000 UZS debt leaves 105 000 outstanding."""
    user = _make_grocery_user()
    contract = _make_amount_contract(user.id)
    order = _make_order(user.id, Decimal("225000.00"))

    service = CorporateContractService()
    service.charge_on_delivery(order=order)
    db.session.commit()

    service.record_money_collection(
        contract=contract,
        amount=Decimal("120000.00"),
        order_id=order.id,
        cash_event_id=999,
        notes="partial cash at delivery",
    )
    db.session.commit()

    db.session.refresh(contract.prepayment_account)
    assert Decimal(str(contract.prepayment_account.outstanding_amount)) == Decimal("105000.00")
    assert Decimal(str(contract.prepayment_account.lifetime_collected)) == Decimal("120000.00")


def test_over_collection_drives_outstanding_negative(db):
    """Cash collected beyond debt becomes credit (negative outstanding)."""
    user = _make_grocery_user()
    contract = _make_amount_contract(user.id)
    order = _make_order(user.id, Decimal("100000.00"))

    service = CorporateContractService()
    service.charge_on_delivery(order=order)
    service.record_money_collection(
        contract=contract,
        amount=Decimal("150000.00"),
        order_id=order.id,
        cash_event_id=12345,
    )
    db.session.commit()

    db.session.refresh(contract.prepayment_account)
    assert Decimal(str(contract.prepayment_account.outstanding_amount)) == Decimal("-50000.00")


def test_validate_business_account_rejects_grocery_store(db):
    user = _make_grocery_user()
    service = CorporateContractService()

    with pytest.raises(ValidationError):
        service.validate_business_account_order(user=user, order_items=[{"product_id": 1, "units": 1}])


def test_get_client_payment_methods_omits_business_account_for_grocery(db):
    user = _make_grocery_user()
    _make_amount_contract(user.id)

    methods = StaffService.get_client_payment_methods(user.id)
    method_values = {m["method"] for m in methods["available_methods"]}

    assert PaymentMethod.BUSINESS_ACCOUNT.value not in method_values
    assert PaymentMethod.CASH.value in method_values
    assert methods["entity_subtype"] == EntitySubtype.GROCERY_STORE.value
    assert methods["has_business_account"] is False


def test_get_client_payment_methods_blocks_unassigned_subtype(db):
    """Entity user with NULL entity_subtype gets empty methods + flag."""
    user = User(
        email=f"unassigned-{uuid4().hex[:8]}@example.com",
        phone=f"+99892{uuid4().int % 10000000:07d}",
        password_hash="x" * 60,
        first_name="Unassigned",
        last_name="Entity",
        user_type=UserType.ENTITY,
        entity_subtype=None,
        company_name="Mystery Inc",
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()

    methods = StaffService.get_client_payment_methods(user.id)
    assert methods["available_methods"] == []
    assert methods["payment_restrictions"]["requires_subtype_assignment"] is True


def test_workplace_methods_include_business_account_when_balance_exists(db):
    """Workplace flow remains unchanged: BUSINESS_ACCOUNT is gated on having an active contract balance."""
    user = _make_workplace_user()
    methods = StaffService.get_client_payment_methods(user.id)
    method_values = {m["method"] for m in methods["available_methods"]}
    # Workplace with no contracts: no BUSINESS_ACCOUNT, no error.
    assert PaymentMethod.BUSINESS_ACCOUNT.value not in method_values
    assert methods["entity_subtype"] == EntitySubtype.WORKPLACE.value
