"""Instant COD confirmation for returning customers.

A COD order from a customer with >=1 DELIVERED order lands CONFIRMED at
creation; first-time COD customers stay PENDING (celery auto-confirms).
"""
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from business_app.models.order import Order, OrderStatusHistory
from business_app.models.user import UserAddress
from business_app.services.order_service import OrderService
from shared.enums import OrderStatus, PaymentMethod


def _address_for(db, user):
    address = UserAddress(
        user_id=user.id,
        title="Home",
        full_address="Street 1",
        street_address="Street 1",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()
    return address


def _make_order(db, user, address, status, number, payment_method=PaymentMethod.CASH):
    order = Order(
        order_number=number,
        user_id=user.id,
        status=status,
        subtotal=Decimal("10000"),
        delivery_fee=Decimal("0"),
        total_amount=Decimal("10000"),
        delivery_address_id=address.id,
        payment_method=payment_method,
        order_source="web",
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.commit()
    return order


@pytest.mark.integration
@pytest.mark.order
class TestCustomerHasDeliveredOrder:
    def test_true_when_user_has_a_delivered_order(self, app, db, sample_user):
        address = _address_for(db, sample_user)
        _make_order(db, sample_user, address, OrderStatus.DELIVERED, "ORD-DLV-1")

        assert OrderService()._customer_has_delivered_order(sample_user.id) is True

    def test_false_when_user_has_no_orders(self, app, db, sample_user):
        assert OrderService()._customer_has_delivered_order(sample_user.id) is False

    def test_false_when_user_has_only_non_delivered_orders(self, app, db, sample_user):
        address = _address_for(db, sample_user)
        _make_order(db, sample_user, address, OrderStatus.CANCELLED, "ORD-CAN-1")
        _make_order(db, sample_user, address, OrderStatus.PENDING, "ORD-PEN-1")

        assert OrderService()._customer_has_delivered_order(sample_user.id) is False


def _cod_order_data(product, address, payment_method="cash"):
    return {
        "items": [{"product_id": product.id, "quantity": 2}],
        "delivery_address": {
            "delivery_address_id": address.id,
            "street": address.street_address,
            "latitude": address.latitude,
            "longitude": address.longitude,
        },
        "payment_method": payment_method,
    }


def _arm_inventory(mock_inventory_service, product):
    mock_inventory_service.check_multiple_products_availability.return_value = [
        SimpleNamespace(
            product_id=product.id,
            requested_quantity=2,
            available_quantity=100,
            reserved_quantity=0,
            is_available=True,
            reason="Available",
        )
    ]
    mock_inventory_service.reserve_inventory.return_value = {"success": True, "expires_at": None}


@pytest.mark.integration
@pytest.mark.order
class TestInstantCodConfirmation:
    def test_returning_customer_cod_order_lands_confirmed(
        self, app, db, sample_user, sample_product, mock_inventory_service
    ):
        # Returning customer: one prior DELIVERED order.
        address = _address_for(db, sample_user)
        _make_order(db, sample_user, address, OrderStatus.DELIVERED, "ORD-DLV-PRIOR")
        _arm_inventory(mock_inventory_service, sample_product)
        service = OrderService(inventory_service=mock_inventory_service)

        with patch(
            "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
            return_value=None,
        ), patch.object(OrderService, "_send_order_notification") as mock_notify:
            order = service.create_order(
                sample_user.id, _cod_order_data(sample_product, address)
            )

        db.session.refresh(order)
        assert order.status == OrderStatus.CONFIRMED
        # Delivery record was created by the confirmation side-effects.
        assert order.delivery is not None
        # Status history records the PENDING -> CONFIRMED transition (persisted).
        history = OrderStatusHistory.query.filter_by(order_id=order.id).all()
        assert any(
            h.old_status == OrderStatus.PENDING and h.new_status == OrderStatus.CONFIRMED
            for h in history
        )
        # The confirmed notification fired.
        assert any(
            c.args[1] == "status_changed_confirmed" for c in mock_notify.call_args_list
        )

    def test_first_time_customer_cod_order_stays_pending(
        self, app, db, sample_user, sample_product, mock_inventory_service
    ):
        # No prior delivered order -> first-time customer.
        address = _address_for(db, sample_user)
        _arm_inventory(mock_inventory_service, sample_product)
        service = OrderService(inventory_service=mock_inventory_service)

        with patch(
            "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
            return_value=None,
        ):
            order = service.create_order(
                sample_user.id, _cod_order_data(sample_product, address)
            )

        db.session.refresh(order)
        assert order.status == OrderStatus.PENDING

    def test_customer_with_only_cancelled_orders_stays_pending(
        self, app, db, sample_user, sample_product, mock_inventory_service
    ):
        address = _address_for(db, sample_user)
        _make_order(db, sample_user, address, OrderStatus.CANCELLED, "ORD-CAN-PRIOR")
        _arm_inventory(mock_inventory_service, sample_product)
        service = OrderService(inventory_service=mock_inventory_service)

        with patch(
            "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
            return_value=None,
        ):
            order = service.create_order(
                sample_user.id, _cod_order_data(sample_product, address)
            )

        db.session.refresh(order)
        assert order.status == OrderStatus.PENDING

    def test_returning_customer_electronic_order_stays_pending(
        self, app, db, sample_user, sample_product, mock_inventory_service
    ):
        # Returning customer but paying electronically -> rule is COD-only.
        address = _address_for(db, sample_user)
        _make_order(db, sample_user, address, OrderStatus.DELIVERED, "ORD-DLV-E")
        _arm_inventory(mock_inventory_service, sample_product)
        service = OrderService(inventory_service=mock_inventory_service)

        with patch(
            "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
            return_value=None,
        ), patch(
            "business_app.services.payment_service.PaymentService.initialize_order_payment",
            return_value=None,
        ):
            order = service.create_order(
                sample_user.id, _cod_order_data(sample_product, address, payment_method="click")
            )

        db.session.refresh(order)
        assert order.status == OrderStatus.PENDING

    def test_confirmation_failure_leaves_order_pending(
        self, app, db, sample_user, sample_product, mock_inventory_service
    ):
        # Returning customer, but confirmation blows up -> graceful fallback.
        address = _address_for(db, sample_user)
        _make_order(db, sample_user, address, OrderStatus.DELIVERED, "ORD-DLV-FAIL")
        _arm_inventory(mock_inventory_service, sample_product)
        service = OrderService(inventory_service=mock_inventory_service)

        with patch(
            "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
            return_value=None,
        ), patch.object(
            OrderService, "update_order_status", side_effect=RuntimeError("boom")
        ):
            order = service.create_order(
                sample_user.id, _cod_order_data(sample_product, address)
            )

        db.session.refresh(order)
        assert order.status == OrderStatus.PENDING
        # No partial confirmation was persisted.
        assert (
            OrderStatusHistory.query.filter_by(
                order_id=order.id, new_status=OrderStatus.CONFIRMED
            ).count()
            == 0
        )

    def test_trust_check_failure_leaves_order_pending(
        self, app, db, sample_user, sample_product, mock_inventory_service
    ):
        # A failure in the trust lookup must not fail order creation.
        address = _address_for(db, sample_user)
        _arm_inventory(mock_inventory_service, sample_product)
        service = OrderService(inventory_service=mock_inventory_service)

        with patch(
            "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
            return_value=None,
        ), patch.object(
            OrderService, "_customer_has_delivered_order", side_effect=RuntimeError("db down")
        ):
            order = service.create_order(
                sample_user.id, _cod_order_data(sample_product, address)
            )

        db.session.refresh(order)
        assert order.status == OrderStatus.PENDING
