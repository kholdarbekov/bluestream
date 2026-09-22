"""Regression: OrderService.create_order must not crash with an
``UnboundLocalError`` when order_data omits ``payment_method``.

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

Task 2 (payment-method SSOT) additionally closed the door this bug lived
behind: a caller that omits ``payment_method`` and does not qualify for the
business-account default must now get a clean ``ValidationError`` instead of
either crashing on ``UnboundLocalError`` OR silently persisting a NULL
``orders.payment_method`` (the original assertion here). An explicit method is
supplied below so this regression test keeps exercising the real
``create_order`` path end-to-end.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sqlalchemy.exc import IntegrityError

from business_app.models.order import Order
from business_app.models.user import UserAddress
from business_app.services.order_service import OrderService
from business_app.utils.exceptions import ValidationError
from shared.enums import PaymentMethod


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
    """order_data with an explicit ``payment_method`` (mirrors subscription
    billing after Task 4) must create the order without hitting the historical
    ``UnboundLocalError``. Omitting ``payment_method`` entirely is covered by
    ``tests/unit/test_subscription_order_parity.py`` — post-Task-2, that now
    correctly raises ``ValidationError`` instead of persisting NULL."""
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
    ), patch(
        "business_app.services.payment_service.PaymentService.initialize_order_payment",
        return_value=None,
    ):
        order = service.create_order(sample_user.id, order_data)

    db.session.refresh(order)
    assert order.id is not None
    assert order.payment_method is PaymentMethod.CASH


def _order_data(sample_product, delivery_address, **extra):
    data = {
        "items": [{"product_id": sample_product.id, "quantity": 2}],
        "delivery_address": {
            "delivery_address_id": delivery_address.id,
            "street": delivery_address.street_address,
            "latitude": delivery_address.latitude,
            "longitude": delivery_address.longitude,
        },
        "payment_method": "cash",
    }
    data.update(extra)
    return data


def _service_that_dies_on_an_integrity_error(mock_inventory_service, sample_product):
    """A `create_order` whose LAST in-transaction step trips a DB constraint.

    Any constraint will do — the question under test is what shape the failure reaches the
    caller in, not which index fired.
    """
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
    return OrderService(inventory_service=mock_inventory_service)


@pytest.mark.integration
@pytest.mark.order
def test_a_non_visit_order_still_reports_an_integrity_error_as_a_validation_error(
    app, db, sample_user, sample_product, mock_inventory_service, delivery_address
):
    """Ruling 54: the sales fix must not change what the other five callers see.

    `create_order`'s `except` arm was widened to let an IntegrityError escape, because
    `VisitService.place_order` has to tell `uq_orders_visit_id` apart from a bad basket. Every
    OTHER caller is written against the historical wrap: subscription billing
    (`subscription_service.py`) catches `ValidationError` alone and would crash the billing run,
    and the three order routes (`api/orders.py`, `api/admin.py`) would turn a former 400 into a
    500. The escape is therefore contained to callers that handed us a `visit_id`.
    """
    service = _service_that_dies_on_an_integrity_error(mock_inventory_service, sample_product)

    with patch(
        "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
        return_value=None,
    ), patch(
        "business_app.services.payment_service.PaymentService.initialize_order_payment",
        side_effect=IntegrityError("INSERT INTO payments ...", {}, Exception("duplicate key")),
    ):
        with pytest.raises(ValidationError) as excinfo:
            service.create_order(sample_user.id, _order_data(sample_product, delivery_address))

    assert "Failed to create order" in str(excinfo.value)
    assert Order.query.filter_by(user_id=sample_user.id).count() == 0


@pytest.mark.integration
@pytest.mark.order
def test_an_agent_order_lets_the_integrity_error_through_so_the_visit_can_answer_409(
    app, db, sample_user, sample_product, mock_inventory_service, delivery_address
):
    """The other side of the same containment: with a visit on the payload the error escapes.

    `VisitService.place_order` catches it, re-reads the visit and answers
    SALES_VISIT_ORDER_EXISTS — which a message-only ValidationError leaves it nothing to do.
    """
    service = _service_that_dies_on_an_integrity_error(mock_inventory_service, sample_product)

    with patch(
        "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
        return_value=None,
    ), patch(
        "business_app.services.payment_service.PaymentService.initialize_order_payment",
        side_effect=IntegrityError("INSERT INTO payments ...", {}, Exception("duplicate key")),
    ):
        with pytest.raises(IntegrityError):
            service.create_order(
                sample_user.id, _order_data(sample_product, delivery_address, visit_id=424242)
            )

    assert Order.query.filter_by(user_id=sample_user.id).count() == 0
