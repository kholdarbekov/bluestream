"""Integration: a new COD order fully covered by the customer's prepaid balance
must be marked PAID at creation, not merely reserved.

Reproduces the prod case for order TG_000201_26: a customer with an unapplied
COD prepayment placed a new CASH order the balance fully covered, but the order
stayed ``is_paid=False`` with a reservation (no ledger application) because
settlement was deferred to delivery. ``create_order`` must now settle a
fully-covered order immediately.
"""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from business_app.models.payment import CashCollectionAllocation, CashCollectionEvent
from business_app.models.user import UserAddress
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.order_service import OrderService
from shared.enums import PaymentStatus


@pytest.fixture
def delivery_address(db, sample_user):
    address = UserAddress(
        user_id=sample_user.id,
        title="Home",
        full_address="Home Street 1",
        street_address="Home Street 1",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()
    return address


@pytest.mark.integration
@pytest.mark.order
def test_create_cod_order_fully_covered_by_prepaid_is_paid_at_creation(
    app, db, sample_user, delivery_driver, sample_product, mock_inventory_service, delivery_address
):
    # Seed a large unapplied COD prepayment surplus that fully covers any order.
    event = CashCollectionEvent(
        customer_id=sample_user.id,
        collector_user_id=delivery_driver.id,
        recorded_by_user_id=delivery_driver.id,
        amount=Decimal("1000000.00"),
        currency="UZS",
        source="standalone_meeting",
        occurred_at=datetime.now(UTC),
        notes="Seeded prepayment surplus",
        unapplied_amount=Decimal("1000000.00"),
    )
    db.session.add(event)
    db.session.commit()

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
            "delivery_address_id": delivery_address.id,
            "street": delivery_address.street_address,
            "latitude": delivery_address.latitude,
            "longitude": delivery_address.longitude,
        },
        "payment_method": "cash",
    }

    with patch(
        "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
        return_value=None,
    ):
        order = service.create_order(sample_user.id, order_data)

    db.session.refresh(order)
    payment = order.payment
    db.session.refresh(payment)

    total = Decimal(str(order.total_amount))
    assert total > Decimal("0.00")

    # The order is settled from prepaid credit at creation.
    assert order.is_paid is True
    assert payment.status == PaymentStatus.COMPLETED
    assert Decimal(str(payment.amount_collected)) == total
    assert Decimal(str(payment.outstanding_amount)) == Decimal("0.00")
    assert payment.collected_by == delivery_driver.id
    assert payment.provider_data.get("cod_prepayment_reserved_amount") == 0.0

    # The cash collection ledger carries an APPLIED prepaid_credit allocation
    # (not just a reservation), tagged for pre-delivery refund.
    alloc = CashCollectionAllocation.query.filter_by(
        payment_id=payment.id, reversed_at=None
    ).one()
    assert alloc.allocation_mode == "prepaid_credit"
    assert alloc.allocation_metadata.get("settled_pre_delivery") is True

    # The customer's prepaid balance dropped by exactly the order total.
    remaining = CashCollectionService().get_customer_prepaid_balance(sample_user.id)
    assert remaining == Decimal("1000000.00") - total
