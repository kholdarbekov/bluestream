"""Regression tests for driver workload consistency across admin and staff flows."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.user import User
from business_app.services.admin_delivery_service import AdminDeliveryService
from business_app.services.staff_service import StaffService
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, UserRole, UserStatus, UserType
from business_app.utils.exceptions import ValidationError
from business_app.utils.password_security import hash_password


def _create_order(db, user_id: int, order_number: str) -> Order:
    order = Order(
        user_id=user_id,
        order_number=order_number,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("15000.00"),
        delivery_fee=Decimal("3000.00"),
        total_amount=Decimal("18000.00"),
        payment_method=PaymentMethod.CASH,
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.commit()
    return order


def _create_driver_user(db, *, phone: str, email: str, first_name: str) -> User:
    user = User(
        email=email,
        phone=phone,
        password_hash=hash_password("DriverPassword123!"),
        first_name=first_name,
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        status=UserStatus.ACTIVE,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.commit()
    return user


def _create_delivery_person(
    db,
    user: User,
    *,
    current_active_deliveries: int,
    max_concurrent_deliveries: int = 3,
) -> DeliveryPerson:
    profile = DeliveryPerson(
        user_id=user.id,
        full_name=user.full_name,
        phone=user.phone,
        email=user.email,
        is_active=True,
        is_available=True,
        current_active_deliveries=current_active_deliveries,
        max_concurrent_deliveries=max_concurrent_deliveries,
    )
    db.session.add(profile)
    db.session.commit()
    return profile


def _create_delivery(
    db,
    order_id: int,
    *,
    delivery_person_id=None,
    status: DeliveryStatus = DeliveryStatus.SCHEDULED,
) -> Delivery:
    delivery = Delivery(
        order_id=order_id,
        delivery_person_id=delivery_person_id,
        status=status,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def test_accept_order_uses_live_count_instead_of_stale_cached_counter(db, sample_user, delivery_driver):
    profile = _create_delivery_person(
        db,
        delivery_driver,
        current_active_deliveries=9,
        max_concurrent_deliveries=3,
    )
    order = _create_order(db, sample_user.id, "ORD-WORKLOAD-1")
    delivery = _create_delivery(db, order.id, status=DeliveryStatus.PENDING)

    accepted = StaffService.accept_order(delivery.id, delivery_driver.id)

    db.session.refresh(profile)
    db.session.refresh(accepted)

    assert accepted.delivery_person_id == delivery_driver.id
    assert accepted.status == DeliveryStatus.ASSIGNED
    assert profile.current_active_deliveries == 1
    assert StaffService.get_active_delivery_count(delivery_driver.id) == 1


def test_accept_order_blocks_when_live_active_count_reaches_capacity(db, sample_user, delivery_driver):
    _create_delivery_person(
        db,
        delivery_driver,
        current_active_deliveries=0,
        max_concurrent_deliveries=1,
    )
    existing_order = _create_order(db, sample_user.id, "ORD-WORKLOAD-2A")
    target_order = _create_order(db, sample_user.id, "ORD-WORKLOAD-2B")
    _create_delivery(
        db,
        existing_order.id,
        delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.ASSIGNED,
    )
    target_delivery = _create_delivery(db, target_order.id, status=DeliveryStatus.PENDING)

    with pytest.raises(ValidationError, match="Maximum concurrent deliveries"):
        StaffService.accept_order(target_delivery.id, delivery_driver.id)


def test_admin_reassign_uses_live_workload_and_resyncs_cached_counters(
    db,
    sample_user,
    delivery_driver,
    admin_user,
):
    old_profile = _create_delivery_person(
        db,
        delivery_driver,
        current_active_deliveries=7,
        max_concurrent_deliveries=3,
    )
    new_driver = _create_driver_user(
        db,
        phone="+998901234570",
        email="driver2@example.com",
        first_name="Second",
    )
    new_profile = _create_delivery_person(
        db,
        new_driver,
        current_active_deliveries=5,
        max_concurrent_deliveries=1,
    )
    order = _create_order(db, sample_user.id, "ORD-WORKLOAD-3")
    delivery = _create_delivery(
        db,
        order.id,
        delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.ASSIGNED,
    )

    updated_delivery = AdminDeliveryService.reassign_delivery(
        delivery.id,
        new_driver.id,
        admin_user.id,
    )

    db.session.refresh(old_profile)
    db.session.refresh(new_profile)
    db.session.refresh(updated_delivery)

    assert updated_delivery.delivery_person_id == new_driver.id
    assert old_profile.current_active_deliveries == 0
    assert new_profile.current_active_deliveries == 1
