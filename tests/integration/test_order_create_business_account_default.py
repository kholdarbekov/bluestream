"""A qualifying workplace-entity order created with NO explicit payment_method
must default to business_account AND reserve prepaid units."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from business_app import db
from business_app.models.corporate import (
    CorporateContract,
    CorporateContractProductPrice,
    CorporateContractStatus,
    CorporatePrepaymentAccount,
    CorporatePrepaymentBalance,
    CorporatePrepaymentEventType,
    CorporatePrepaymentLedger,
)
from business_app.models.user import User, UserAddress
from business_app.services.order_service import OrderService
from shared.enums import (
    CorporateContractTrackingMode,
    EntitySubtype,
    PaymentMethod,
    UserRole,
    UserType,
)


@pytest.fixture
def workplace_user(db):
    user = User(
        email=f"wp-{uuid4().hex[:8]}@example.com",
        phone=f"+99895{uuid4().int % 10000000:07d}",
        password_hash="x" * 60,
        first_name="Work",
        last_name="Place",
        user_type=UserType.ENTITY,
        entity_subtype=EntitySubtype.WORKPLACE,
        company_name="Test Workplace",
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def workplace_address(db, workplace_user):
    address = UserAddress(
        user_id=workplace_user.id,
        title="Office",
        full_address="Office 1",
        street_address="Office 1",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()
    return address


@pytest.fixture
def covered_contract(db, workplace_user, sample_product):
    contract = CorporateContract(
        user_id=workplace_user.id,
        contract_number=f"C-{uuid4().hex[:10]}",
        name="Coverage Contract",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1),
        currency="UZS",
        is_active=True,
        tracking_mode=CorporateContractTrackingMode.UNITS,
    )
    db.session.add(contract)
    db.session.flush()
    db.session.add(
        CorporateContractProductPrice(
            contract_id=contract.id,
            product_id=sample_product.id,
            unit_price=Decimal("18000.00"),
            is_prepayment_eligible=True,
            is_active=True,
        )
    )
    account = CorporatePrepaymentAccount(contract_id=contract.id, is_active=True)
    db.session.add(account)
    db.session.flush()
    db.session.add(
        CorporatePrepaymentBalance(
            account_id=account.id,
            product_id=sample_product.id,
            prepaid_units=Decimal("50.00"),
            reserved_units=Decimal("0.00"),
            consumed_units=Decimal("0.00"),
            is_active=True,
        )
    )
    db.session.commit()
    return contract


@pytest.mark.integration
@pytest.mark.order
def test_qualifying_order_without_method_defaults_to_business_account(
    app, db, workplace_user, sample_product, mock_inventory_service, workplace_address, covered_contract
):
    mock_inventory_service.check_multiple_products_availability.return_value = [
        SimpleNamespace(
            product_id=sample_product.id,
            requested_quantity=2,
            available_quantity=100,
            reserved_quantity=0,
            is_available=True,
            reason="Available",
        )
    ]
    mock_inventory_service.reserve_inventory.return_value = {"success": True, "expires_at": None}

    service = OrderService(inventory_service=mock_inventory_service)
    order_data = {
        "items": [{"product_id": sample_product.id, "quantity": 2}],
        "delivery_address": {
            "delivery_address_id": workplace_address.id,
            "street": workplace_address.street_address,
            "latitude": workplace_address.latitude,
            "longitude": workplace_address.longitude,
        },
        # Intentionally NO "payment_method".
    }

    # Isolate defaulting + gating from payment-row side effects.
    with patch(
        "business_app.services.payment_service.PaymentService.initialize_order_payment",
        return_value=None,
    ):
        order = service.create_order(workplace_user.id, order_data)

    db.session.refresh(order)
    assert order.payment_method == PaymentMethod.BUSINESS_ACCOUNT

    reserve_rows = CorporatePrepaymentLedger.query.filter_by(
        order_id=order.id, event_type=CorporatePrepaymentEventType.RESERVE
    ).all()
    assert len(reserve_rows) == 1
    assert Decimal(str(reserve_rows[0].units)) == Decimal("2.00")


@pytest.mark.integration
@pytest.mark.order
def test_explicit_cash_on_qualifying_order_is_respected_and_not_reserved(
    app, db, workplace_user, sample_product, mock_inventory_service, workplace_address, covered_contract
):
    mock_inventory_service.check_multiple_products_availability.return_value = [
        SimpleNamespace(
            product_id=sample_product.id,
            requested_quantity=2,
            available_quantity=100,
            reserved_quantity=0,
            is_available=True,
            reason="Available",
        )
    ]
    mock_inventory_service.reserve_inventory.return_value = {"success": True, "expires_at": None}

    service = OrderService(inventory_service=mock_inventory_service)
    order_data = {
        "items": [{"product_id": sample_product.id, "quantity": 2}],
        "delivery_address": {
            "delivery_address_id": workplace_address.id,
            "street": workplace_address.street_address,
            "latitude": workplace_address.latitude,
            "longitude": workplace_address.longitude,
        },
        "payment_method": "cash",  # explicit — must be respected
    }

    with patch(
        "business_app.services.payment_service.PaymentService.initialize_order_payment",
        return_value=None,
    ):
        order = service.create_order(workplace_user.id, order_data)

    db.session.refresh(order)
    assert order.payment_method == PaymentMethod.CASH
    assert (
        CorporatePrepaymentLedger.query.filter_by(
            order_id=order.id, event_type=CorporatePrepaymentEventType.RESERVE
        ).count()
        == 0
    )


@pytest.mark.integration
@pytest.mark.order
def test_qualifying_order_with_default_suppressed_stays_none(
    app, db, workplace_user, sample_product, mock_inventory_service, workplace_address, covered_contract
):
    """Callers that opt out (e.g. subscription billing) must NOT get the
    business_account default applied, even on an otherwise-qualifying order."""
    mock_inventory_service.check_multiple_products_availability.return_value = [
        SimpleNamespace(
            product_id=sample_product.id,
            requested_quantity=2,
            available_quantity=100,
            reserved_quantity=0,
            is_available=True,
            reason="Available",
        )
    ]
    mock_inventory_service.reserve_inventory.return_value = {"success": True, "expires_at": None}

    service = OrderService(inventory_service=mock_inventory_service)
    order_data = {
        "items": [{"product_id": sample_product.id, "quantity": 2}],
        "delivery_address": {
            "delivery_address_id": workplace_address.id,
            "street": workplace_address.street_address,
            "latitude": workplace_address.latitude,
            "longitude": workplace_address.longitude,
        },
        # Intentionally NO "payment_method".
    }

    with patch(
        "business_app.services.payment_service.PaymentService.initialize_order_payment",
        return_value=None,
    ):
        order = service.create_order(workplace_user.id, order_data, apply_payment_method_default=False)

    db.session.refresh(order)
    assert order.payment_method is None
    assert (
        CorporatePrepaymentLedger.query.filter_by(
            order_id=order.id, event_type=CorporatePrepaymentEventType.RESERVE
        ).count()
        == 0
    )
