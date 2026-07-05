from datetime import datetime, timezone
from decimal import Decimal

import pytest

from business_app import db
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.payment import CashCollectionAllocation, Payment
from business_app.services.cash_collection_service import CashCollectionService
from business_app.utils.exceptions import ValidationError
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod


@pytest.fixture
def driver_profile(db, delivery_driver):
    profile = DeliveryPerson(
        user_id=delivery_driver.id, full_name="Driver", phone=delivery_driver.phone,
        email=delivery_driver.email, is_active=True, is_available=True,
    )
    db.session.add(profile)
    db.session.commit()
    return profile


@pytest.fixture
def delivered_cod(db, sample_order, delivery_driver):
    sample_order.status = OrderStatus.DELIVERED
    sample_order.payment_method = PaymentMethod.CASH
    sample_order.total_amount = Decimal("54000")
    sample_order.paid_at = datetime.now(timezone.utc)
    delivery = Delivery(
        order_id=sample_order.id, delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.DELIVERED, scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="09:00-12:00", actual_delivery_time=datetime.now(timezone.utc),
    )
    db.session.add(delivery)
    db.session.commit()
    return sample_order, delivery


def _seed(service, sample_user, delivery_driver, order, delivery, amount):
    service.ensure_cod_payment_for_order(order)
    return service.post_collection(
        customer_id=sample_user.id, amount=Decimal(amount), source="delivery_completion",
        collector_user_id=delivery_driver.id, recorded_by_user_id=delivery_driver.id,
        order_id=order.id, delivery_id=delivery.id, notes="seed",
    )


def _live_allocation(payment_id, event_id):
    return CashCollectionAllocation.query.filter_by(
        payment_id=payment_id, cash_collection_event_id=event_id, reversed_at=None,
    ).one()


def test_reverse_allocation_moves_collected_cash_to_customer_credit(
    db, sample_user, delivery_driver, driver_profile, delivered_cod
):
    order, delivery = delivered_cod
    svc = CashCollectionService()
    event = _seed(svc, sample_user, delivery_driver, order, delivery, "54000")

    payment = Payment.query.filter_by(order_id=order.id).first()
    allocation = _live_allocation(payment.id, event.id)

    prepaid_before = svc.get_customer_prepaid_balance(sample_user.id)
    unapplied_before = Decimal(str(event.unapplied_amount))
    amount_collected_before = Decimal(str(payment.amount_collected))

    result = svc.reverse_allocation_to_payment(
        allocation.id, reversed_by_user_id=delivery_driver.id, reason="reclassify to credit",
        commit=True,
    )

    assert result.id == allocation.id
    assert result.reversed_at is not None
    assert result.reversed_by_user_id == delivery_driver.id
    assert result.reversal_reason == "reclassify to credit"

    db.session.refresh(event)
    db.session.refresh(payment)

    assert Decimal(str(event.unapplied_amount)) == unapplied_before + Decimal("54000")
    assert Decimal(str(payment.amount_collected)) == amount_collected_before - Decimal("54000")
    assert svc.get_customer_prepaid_balance(sample_user.id) == prepaid_before + Decimal("54000")
    # Driver-cash-session totals must be untouched by construction.
    assert event.driver_cash_session_id is not None


def test_reverse_allocation_already_reversed_raises(
    db, sample_user, delivery_driver, driver_profile, delivered_cod
):
    order, delivery = delivered_cod
    svc = CashCollectionService()
    event = _seed(svc, sample_user, delivery_driver, order, delivery, "54000")
    payment = Payment.query.filter_by(order_id=order.id).first()
    allocation = _live_allocation(payment.id, event.id)

    svc.reverse_allocation_to_payment(
        allocation.id, reversed_by_user_id=delivery_driver.id, reason="first reversal",
        commit=True,
    )

    with pytest.raises(ValidationError):
        svc.reverse_allocation_to_payment(
            allocation.id, reversed_by_user_id=delivery_driver.id, reason="second reversal",
            commit=True,
        )


def test_reverse_allocation_voided_parent_event_raises(
    db, sample_user, delivery_driver, driver_profile, delivered_cod
):
    order, delivery = delivered_cod
    svc = CashCollectionService()
    event = _seed(svc, sample_user, delivery_driver, order, delivery, "54000")
    payment = Payment.query.filter_by(order_id=order.id).first()
    allocation = _live_allocation(payment.id, event.id)

    svc.reverse_collection_event(
        event.id, reversed_by_user_id=delivery_driver.id, reason="void event", commit=True,
    )

    with pytest.raises(ValidationError):
        svc.reverse_allocation_to_payment(
            allocation.id, reversed_by_user_id=delivery_driver.id, reason="too late",
            commit=True,
        )


def test_reverse_allocation_missing_id_raises(db, sample_user):
    svc = CashCollectionService()
    with pytest.raises(ValidationError):
        svc.reverse_allocation_to_payment(
            999999, reversed_by_user_id=sample_user.id, reason="no such allocation",
            commit=True,
        )
