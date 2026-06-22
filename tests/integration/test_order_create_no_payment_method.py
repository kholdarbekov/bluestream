"""Regression: OrderService.create_order must not crash when order_data omits
``payment_method``.

Prod bug (celery_worker, daily ``process_daily_subscription_billing``): a
redundant local ``from shared.enums import PaymentMethod`` inside
``create_order`` made ``PaymentMethod`` a *function-local* for the whole method.
The subscription billing path builds ``order_data`` WITHOUT a ``payment_method``
key, so the guarded local import never ran and the later reference
``if payment_method == PaymentMethod.CASH`` raised
``UnboundLocalError: cannot access local variable 'PaymentMethod'`` — re-raised
as ``ValidationError: Failed to create order``. The existing subscription
billing tests never caught this because they mock ``create_order``; this test
drives the real method with a subscription-style payload (no payment_method).
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from business_app.models.user import UserAddress
from business_app.services.order_service import OrderService


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
def test_create_order_without_payment_method_does_not_raise_unbound_local(
    app, db, sample_user, sample_product, mock_inventory_service, delivery_address
):
    """order_data with no ``payment_method`` key (mirrors subscription billing)
    must create the order, leaving ``order.payment_method`` as None."""
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
        # Intentionally NO "payment_method" — this is what subscription billing sends.
    }

    with patch(
        "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
        return_value=None,
    ), patch(
        "business_app.services.payment_service.PaymentService.initialize_order_payment",
        return_value=None,
    ):
        order = service.create_order(sample_user.id, order_data)

    db.session.refresh(order)
    assert order.id is not None
    assert order.payment_method is None
