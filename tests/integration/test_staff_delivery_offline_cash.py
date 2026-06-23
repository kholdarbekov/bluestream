"""Integration tests for Task 4: cash-at-delivery settles unsuccessful electronic orders.

When a driver marks an unsuccessful-electronic order (CLICK/PAYME/CARD with a
PENDING/CANCELLED/FAILED payment) as delivered with cash_collected > 0, the
service must:
  1. Convert the order+payment to CASH via CashCollectionService.convert_electronic_order_to_cash
  2. Post a delivery_completion collection as normal (existing COD flow)
  3. Leave payment.status == COMPLETED, order.is_paid == True
  4. NOT fire the cod_debt_limit_breached notification (only for true CASH orders)
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.user import UserAddress
from shared.enums import (
    DeliveryStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_address(db, user_id):
    """Create a minimal delivery address for an order."""
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


def _make_click_payment(db, order, *, status=PaymentStatus.PENDING, payment_id_str="click_test"):
    """Create a CLICK payment for an order."""
    payment = Payment(
        order_id=order.id,
        user_id=order.user_id,
        payment_method=PaymentMethod.CLICK,
        status=status,
        amount=order.total_amount,
        amount_collected=Decimal("0.00"),
        outstanding_amount=order.total_amount,
        currency="UZS",
        payment_id=payment_id_str,
    )
    db.session.add(payment)
    return payment


def _setup_electronic_order(db, sample_order, delivery_driver, *, payment_status=PaymentStatus.PENDING, payment_id_str="click_test"):
    """Configure sample_order as an electronic CLICK order in OUT_FOR_DELIVERY, with a delivery."""
    address = _make_address(db, sample_order.user_id)
    sample_order.payment_method = PaymentMethod.CLICK
    sample_order.status = OrderStatus.OUT_FOR_DELIVERY
    sample_order.delivery_address_id = address.id
    db.session.flush()

    payment = _make_click_payment(db, sample_order, status=payment_status, payment_id_str=payment_id_str)

    delivery = Delivery(
        order_id=sample_order.id,
        delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.ARRIVED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()

    return payment, delivery


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def driver_profile(db, delivery_driver):
    """Create the DeliveryPerson profile for delivery_driver."""
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_driver_cash_settles_pending_click_order(
    app, db, delivery_driver, driver_profile, sample_order
):
    """Marking a PENDING-Click order delivered with cash_collected converts it to CASH
    and posts the collection, resulting in a COMPLETED payment and paid order.
    """
    from business_app.services.staff_service import StaffService

    payment, delivery = _setup_electronic_order(
        db, sample_order, delivery_driver,
        payment_status=PaymentStatus.PENDING,
        payment_id_str="click_pending_settle_test",
    )
    order_id = sample_order.id
    payment_id = payment.id

    with patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay"):
        with patch("business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"):
            StaffService.update_delivery_status(
                delivery_id=delivery.id,
                new_status="delivered",
                staff_user_id=delivery_driver.id,
                metadata={"cash_collected": str(sample_order.total_amount)},
            )

    db.session.expire_all()
    fresh_payment = Payment.query.get(payment_id)
    fresh_order = Order.query.get(order_id)

    assert fresh_payment.payment_method == PaymentMethod.CASH
    assert fresh_payment.status == PaymentStatus.COMPLETED
    assert fresh_order.is_paid is True
    assert fresh_payment.collected_by == delivery_driver.id


@pytest.mark.integration
def test_driver_cash_settles_cancelled_click_order(
    app, db, delivery_driver, driver_profile, sample_order
):
    """A CANCELLED Click payment (e.g. timed out) is also settled when driver
    collects cash at delivery — mirrors the canonical prod scenario order 547.
    """
    from business_app.services.staff_service import StaffService

    payment, delivery = _setup_electronic_order(
        db, sample_order, delivery_driver,
        payment_status=PaymentStatus.CANCELLED,
        payment_id_str="click_cancelled_settle_test",
    )
    order_id = sample_order.id
    payment_id = payment.id

    with patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay"):
        with patch("business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"):
            StaffService.update_delivery_status(
                delivery_id=delivery.id,
                new_status="delivered",
                staff_user_id=delivery_driver.id,
                metadata={"cash_collected": str(sample_order.total_amount)},
            )

    db.session.expire_all()
    fresh_payment = Payment.query.get(payment_id)
    fresh_order = Order.query.get(order_id)

    assert fresh_payment.payment_method == PaymentMethod.CASH
    assert fresh_payment.status == PaymentStatus.COMPLETED
    assert fresh_order.is_paid is True
    assert fresh_payment.collected_by == delivery_driver.id


@pytest.mark.integration
def test_driver_zero_cash_does_not_convert_electronic_order(
    app, db, delivery_driver, driver_profile, sample_order
):
    """When cash_collected == 0 the electronic order is NOT converted — the
    conversion only fires when there is a positive cash amount.
    """
    from business_app.services.staff_service import StaffService

    payment, delivery = _setup_electronic_order(
        db, sample_order, delivery_driver,
        payment_status=PaymentStatus.PENDING,
        payment_id_str="click_zero_cash_no_convert",
    )
    payment_id = payment.id

    with patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay"):
        with patch("business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"):
            StaffService.update_delivery_status(
                delivery_id=delivery.id,
                new_status="delivered",
                staff_user_id=delivery_driver.id,
                metadata={"cash_collected": "0.00", "notes": "Customer will pay later"},
            )

    db.session.expire_all()
    fresh_order = Order.query.get(sample_order.id)
    # Payment should remain CLICK/PENDING — no cash collected means no conversion
    fresh_payment = Payment.query.get(payment_id)
    assert fresh_payment.payment_method == PaymentMethod.CLICK
    assert fresh_payment.status == PaymentStatus.PENDING
    assert fresh_order.is_paid is False


@pytest.mark.integration
def test_existing_cash_order_delivery_still_works(
    app, db, delivery_driver, driver_profile, sample_order
):
    """Regression: standard COD delivery must still settle normally after the change."""
    from business_app.services.staff_service import StaffService
    from business_app.services.cash_collection_service import CashCollectionService

    address = _make_address(db, sample_order.user_id)
    sample_order.payment_method = PaymentMethod.CASH
    sample_order.status = OrderStatus.OUT_FOR_DELIVERY
    sample_order.delivery_address_id = address.id
    db.session.flush()

    # Ensure the COD payment exists (mirrors the real flow where it's created at confirm)
    cod_payment = CashCollectionService().ensure_cod_payment_for_order(sample_order)

    delivery = Delivery(
        order_id=sample_order.id,
        delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.ARRIVED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()

    order_id = sample_order.id
    payment_id = cod_payment.id
    total = sample_order.total_amount

    with patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay"):
        with patch("business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"):
            StaffService.update_delivery_status(
                delivery_id=delivery.id,
                new_status="delivered",
                staff_user_id=delivery_driver.id,
                metadata={"cash_collected": str(total)},
            )

    db.session.expire_all()
    fresh_payment = Payment.query.get(payment_id)
    fresh_order = Order.query.get(order_id)

    # COD order fully settled
    assert fresh_payment.payment_method == PaymentMethod.CASH
    assert fresh_payment.status == PaymentStatus.COMPLETED
    assert fresh_order.is_paid is True
    assert fresh_payment.collected_by == delivery_driver.id


@pytest.mark.integration
def test_already_paid_electronic_order_not_converted(
    app, db, delivery_driver, driver_profile, sample_order
):
    """An already-COMPLETED electronic (CLICK) payment must NOT be re-converted
    or double-settled when the driver marks it delivered — even if cash_collected
    metadata is supplied (e.g. stale bot UI).

    The order is already paid; no CashCollectionService.convert_electronic_order_to_cash
    should fire, and payment_method must stay CLICK with status COMPLETED.
    """
    from business_app.services.staff_service import StaffService

    address = _make_address(db, sample_order.user_id)
    sample_order.payment_method = PaymentMethod.CLICK
    sample_order.status = OrderStatus.OUT_FOR_DELIVERY
    sample_order.is_paid = True
    sample_order.delivery_address_id = address.id
    db.session.flush()

    # Already-paid CLICK payment
    payment = _make_click_payment(
        db,
        sample_order,
        status=PaymentStatus.COMPLETED,
        payment_id_str="click_already_paid_no_convert",
    )
    payment.amount_collected = sample_order.total_amount
    payment.outstanding_amount = Decimal("0.00")

    delivery = Delivery(
        order_id=sample_order.id,
        delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.ARRIVED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()

    order_id = sample_order.id
    payment_id = payment.id

    with patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay"):
        with patch("business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"):
            with patch(
                "business_app.services.cash_collection_service.CashCollectionService"
                ".convert_electronic_order_to_cash"
            ) as mock_convert:
                StaffService.update_delivery_status(
                    delivery_id=delivery.id,
                    new_status="delivered",
                    staff_user_id=delivery_driver.id,
                    # Stale metadata: cash_collected supplied even though already paid
                    metadata={"cash_collected": str(sample_order.total_amount)},
                )
                # Conversion must never be called for an already-settled payment
                mock_convert.assert_not_called()

    db.session.expire_all()
    fresh_payment = Payment.query.get(payment_id)
    fresh_order = Order.query.get(order_id)

    # Payment method and status must be unchanged
    assert fresh_payment.payment_method == PaymentMethod.CLICK
    assert fresh_payment.status == PaymentStatus.COMPLETED
    # Order was already paid — stays paid
    assert fresh_order.is_paid is True


@pytest.mark.integration
def test_non_numeric_cash_collected_metadata_safe(
    app, db, delivery_driver, driver_profile, sample_order
):
    """A non-numeric / garbage cash_collected value in metadata must not crash
    the service (no HTTP 500).  It must be silently coerced to "no cash" and
    the electronic order must NOT be converted.
    """
    from business_app.services.staff_service import StaffService

    payment, delivery = _setup_electronic_order(
        db, sample_order, delivery_driver,
        payment_status=PaymentStatus.PENDING,
        payment_id_str="click_garbage_cash_safe",
    )
    payment_id = payment.id

    with patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay"):
        with patch("business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"):
            # Must not raise — the service absorbs the bad value gracefully
            StaffService.update_delivery_status(
                delivery_id=delivery.id,
                new_status="delivered",
                staff_user_id=delivery_driver.id,
                metadata={"cash_collected": "not-a-number", "notes": "corrupt UI value"},
            )

    db.session.expire_all()
    # Non-numeric cash treated as "no cash" → no conversion
    fresh_payment = Payment.query.get(payment_id)
    assert fresh_payment.payment_method == PaymentMethod.CLICK
    assert fresh_payment.status == PaymentStatus.PENDING
