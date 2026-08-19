"""COD orders whose PAYMENT row was re-pointed at an online rail.

`orders.payment_method` gates settlement; `payments.payment_method` drives
allocation. When they diverge, door cash posted as customer credit and the
order stayed unpaid. Pins: the rails move together, and cash at the door
settles the order whatever they say.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.payment import CashCollectionAllocation, Payment
from business_app.models.user import UserAddress
from shared.enums import (
    DeliveryStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)


def _make_address(db, user_id):
    address = UserAddress(
        user_id=user_id,
        title="Home",
        full_address="123 Test Street, Tashkent",
        street_address="123 Test Street",
        city="Tashkent",
        is_default=True,
    )
    db.session.add(address)
    db.session.flush()
    return address


@pytest.fixture
def driver_profile(db, delivery_driver):
    profile = DeliveryPerson(
        user_id=delivery_driver.id,
        full_name="Test Driver",
        phone=delivery_driver.phone,
        email=delivery_driver.email,
        is_active=True,
        is_available=True,
    )
    db.session.add(profile)
    db.session.commit()
    return profile


def _desynced_cod_order(db, order, driver, *, payment_status=PaymentStatus.PENDING):
    """order.payment_method == CASH but the payment row is CLICK — the prod shape."""
    address = _make_address(db, order.user_id)
    order.payment_method = PaymentMethod.CASH
    order.status = OrderStatus.OUT_FOR_DELIVERY
    order.delivery_address_id = address.id
    db.session.flush()

    payment = Payment(
        order_id=order.id,
        user_id=order.user_id,
        payment_method=PaymentMethod.CLICK,
        status=payment_status,
        amount=order.total_amount,
        amount_collected=Decimal("0.00"),
        outstanding_amount=order.total_amount,
        currency="UZS",
        payment_id=f"click_desync_{order.id}",
    )
    db.session.add(payment)

    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver.id,
        status=DeliveryStatus.ARRIVED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return payment, delivery


def _deliver_with_cash(driver, delivery, amount):
    from business_app.services.staff_service import StaffService

    with patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay"):
        with patch("business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"):
            StaffService.update_delivery_status(
                delivery_id=delivery.id,
                new_status="delivered",
                staff_user_id=driver.id,
                metadata={"cash_collected": str(amount)},
            )


# --------------------------------------------------------------------------- #
# 1. The rails must not diverge
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_pay_now_on_cod_order_keeps_order_rail_in_sync(
    matrix_app, db, order_with_address, auth_headers
):
    """POST /payments/create switching a COD order to Click must move BOTH rails.

    Leaving orders.payment_method == cash is what stranded prod TG_000401_26.
    """
    from business_app.services.cash_collection_service import CashCollectionService

    order = order_with_address
    order.payment_method = PaymentMethod.CASH
    db.session.commit()
    cod_payment = CashCollectionService().ensure_cod_payment_for_order(order)
    db.session.commit()
    payment_id = cod_payment.id

    client = matrix_app.test_client()
    resp = client.post(
        "/api/v1/payments/create",
        json={"order_id": order.id, "payment_method": "click"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.get_json()

    db.session.expire_all()
    fresh_order = Order.query.get(order.id)
    fresh_payment = Payment.query.get(payment_id)
    assert fresh_payment.payment_method == PaymentMethod.CLICK
    assert fresh_order.payment_method == PaymentMethod.CLICK


# --------------------------------------------------------------------------- #
# 2. Cash at the door settles the order whatever the rails say
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_full_cash_settles_desynced_order(
    app, db, delivery_driver, driver_profile, sample_order
):
    from business_app.services.cash_collection_service import CashCollectionService

    payment, delivery = _desynced_cod_order(db, sample_order, delivery_driver)
    order_id, payment_id = sample_order.id, payment.id
    customer_id = sample_order.user_id
    total = sample_order.total_amount

    _deliver_with_cash(delivery_driver, delivery, total)

    db.session.expire_all()
    fresh_payment = Payment.query.get(payment_id)
    fresh_order = Order.query.get(order_id)

    assert fresh_payment.payment_method == PaymentMethod.CASH
    assert fresh_payment.status == PaymentStatus.COMPLETED
    assert fresh_payment.amount_collected == total
    assert fresh_payment.outstanding_amount == Decimal("0.00")
    assert fresh_order.is_paid is True
    assert fresh_order.payment_method == PaymentMethod.CASH
    # None of it may leak into prepaid credit.
    assert CashCollectionService().get_customer_prepaid_balance(customer_id) == Decimal("0.00")
    assert (
        CashCollectionAllocation.query.filter_by(payment_id=payment_id).count() == 1
    )


@pytest.mark.integration
def test_partial_cash_settles_desynced_order_partially(
    app, db, delivery_driver, driver_profile, sample_order
):
    """"any amount, less or extra": a short collection allocates what was handed
    over and leaves the remainder outstanding — never customer credit."""
    from business_app.services.cash_collection_service import CashCollectionService

    payment, delivery = _desynced_cod_order(db, sample_order, delivery_driver)
    order_id, payment_id = sample_order.id, payment.id
    customer_id = sample_order.user_id
    total = sample_order.total_amount
    collected = (total / 2).quantize(Decimal("0.01"))

    _deliver_with_cash(delivery_driver, delivery, collected)

    db.session.expire_all()
    fresh_payment = Payment.query.get(payment_id)
    fresh_order = Order.query.get(order_id)

    assert fresh_payment.payment_method == PaymentMethod.CASH
    assert fresh_payment.amount_collected == collected
    assert fresh_payment.outstanding_amount == total - collected
    assert fresh_payment.status == PaymentStatus.PARTIALLY_PAID
    assert fresh_order.is_paid is False
    assert CashCollectionService().get_customer_prepaid_balance(customer_id) == Decimal("0.00")


@pytest.mark.integration
def test_over_collection_on_desynced_order_settles_then_credits_surplus(
    app, db, delivery_driver, driver_profile, sample_order
):
    from business_app.services.cash_collection_service import CashCollectionService

    payment, delivery = _desynced_cod_order(db, sample_order, delivery_driver)
    order_id, payment_id = sample_order.id, payment.id
    customer_id = sample_order.user_id
    total = sample_order.total_amount
    surplus = Decimal("5000.00")

    _deliver_with_cash(delivery_driver, delivery, total + surplus)

    db.session.expire_all()
    fresh_payment = Payment.query.get(payment_id)
    fresh_order = Order.query.get(order_id)

    assert fresh_payment.payment_method == PaymentMethod.CASH
    assert fresh_payment.status == PaymentStatus.COMPLETED
    assert fresh_order.is_paid is True
    assert CashCollectionService().get_customer_prepaid_balance(customer_id) == surplus


# --------------------------------------------------------------------------- #
# 3. Remediating an order already stranded by the old behaviour
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_remediation_settles_an_already_stranded_order(
    app, db, delivery_driver, driver_profile, sample_order
):
    """scripts/remediate_cod_rail_desync.py's two calls, on the shape prod left:
    delivered, unpaid, with the door cash sitting as unapplied credit."""
    from uuid import uuid4

    from business_app.models.payment import CashCollectionEvent
    from business_app.services.cash_collection_service import CashCollectionService
    from shared.enums import CashCollectionSource

    payment, delivery = _desynced_cod_order(db, sample_order, delivery_driver)
    total = sample_order.total_amount
    sample_order.status = OrderStatus.DELIVERED
    delivery.status = DeliveryStatus.DELIVERED
    delivery.cash_collected = total
    event = CashCollectionEvent(
        event_id=str(uuid4()),
        customer_id=sample_order.user_id,
        collector_user_id=delivery_driver.id,
        recorded_by_user_id=delivery_driver.id,
        order_id=sample_order.id,
        delivery_id=delivery.id,
        amount=total,
        unapplied_amount=total,
        currency="UZS",
        source=CashCollectionSource.DELIVERY_COMPLETION,
        occurred_at=datetime.now(UTC),
    )
    db.session.add(event)
    db.session.commit()

    order_id, payment_id, event_id = sample_order.id, payment.id, event.id
    customer_id = sample_order.user_id
    service = CashCollectionService()
    assert service.get_customer_prepaid_balance(customer_id) == total

    service.convert_electronic_order_to_cash(
        sample_order, actor_user_id=None, reason="cash_collected_at_delivery"
    )
    service.settle_payment_from_customer_credit(payment, actor_user_id=None)
    db.session.commit()

    db.session.expire_all()
    fresh_payment = Payment.query.get(payment_id)
    fresh_order = Order.query.get(order_id)
    fresh_event = CashCollectionEvent.query.get(event_id)

    assert fresh_payment.payment_method == PaymentMethod.CASH
    assert fresh_payment.status == PaymentStatus.COMPLETED
    assert fresh_payment.amount_collected == total
    assert fresh_payment.collected_by == delivery_driver.id
    assert fresh_order.is_paid is True
    assert fresh_event.unapplied_amount == Decimal("0.00")
    assert service.get_customer_prepaid_balance(customer_id) == Decimal("0.00")
    assert CashCollectionAllocation.query.filter_by(payment_id=payment_id).count() == 1
