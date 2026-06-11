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
from business_app.utils.exceptions import InvalidStateTransition, ValidationError
from business_app.utils.password_security import hash_password
from business_app.utils.state_validators import assert_unassigned_for_pool_status


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


def test_accept_order_no_longer_caps_concurrent_deliveries(db, sample_user, delivery_driver):
    """Cap removed when implicit route optimization shipped — drivers may now
    claim freely beyond `max_concurrent_deliveries` and the optimizer handles
    ordering. The column is preserved for a possible future per-driver admin
    override but is no longer enforced at the accept site."""
    profile = _create_delivery_person(
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

    accepted = StaffService.accept_order(target_delivery.id, delivery_driver.id)

    db.session.refresh(profile)
    db.session.refresh(accepted)
    assert accepted.delivery_person_id == delivery_driver.id
    assert accepted.status == DeliveryStatus.ASSIGNED
    # Live count is now 2 (existing + target) even though max_concurrent was 1.
    assert StaffService.get_active_delivery_count(delivery_driver.id) == 2


def test_accept_order_allows_same_driver_to_reaccept_unclaimed_delivery(db, sample_user, delivery_driver):
    """A delivery left in SCHEDULED while delivery_person_id still points at the
    same driver (e.g. after a manual/return-to-pool edit) must be re-acceptable
    by that driver. Previously this raised STAFF_DELIVERY_ALREADY_TAKEN against
    the driver themselves, stranding the delivery in a state no screen surfaced."""
    _create_delivery_person(db, delivery_driver, current_active_deliveries=0)
    order = _create_order(db, sample_user.id, "ORD-REACCEPT-1")
    delivery = _create_delivery(
        db,
        order.id,
        delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.SCHEDULED,
    )

    accepted = StaffService.accept_order(delivery.id, delivery_driver.id)

    db.session.refresh(accepted)
    assert accepted.delivery_person_id == delivery_driver.id
    assert accepted.status == DeliveryStatus.ASSIGNED


def test_accept_order_rejects_delivery_assigned_to_another_driver(db, sample_user, delivery_driver):
    """The same-driver allowance must not let a second driver steal a delivery
    already claimed by someone else."""
    other_driver = _create_driver_user(
        db, phone="+998901234571", email="other-driver@example.com", first_name="Other"
    )
    order = _create_order(db, sample_user.id, "ORD-REACCEPT-2")
    delivery = _create_delivery(
        db,
        order.id,
        delivery_person_id=other_driver.id,
        status=DeliveryStatus.SCHEDULED,
    )

    with pytest.raises(ValidationError) as exc_info:
        StaffService.accept_order(delivery.id, delivery_driver.id)
    assert exc_info.value.error_code == "STAFF_DELIVERY_ALREADY_TAKEN"


def test_accept_order_rejects_non_claimable_status(db, sample_user, delivery_driver):
    """Only unclaimed (scheduled/pending) deliveries may be accepted. A terminal
    FAILED delivery must be rejected, not silently reset to ASSIGNED."""
    order = _create_order(db, sample_user.id, "ORD-REACCEPT-3")
    delivery = _create_delivery(db, order.id, status=DeliveryStatus.FAILED)

    with pytest.raises(ValidationError) as exc_info:
        StaffService.accept_order(delivery.id, delivery_driver.id)
    assert exc_info.value.error_code == "STAFF_DELIVERY_NOT_CLAIMABLE"


def test_return_delivery_to_pool_clears_driver_and_restores_order(db, sample_user, delivery_driver, admin_user):
    """Returning a failed delivery to the pool must clear the driver, reset the
    delivery to SCHEDULED, restore the order to a pool-eligible status, clear the
    stale failure reason, preserve delivery_attempts, and record history."""
    _create_delivery_person(db, delivery_driver, current_active_deliveries=1)
    order = _create_order(db, sample_user.id, "ORD-RETURNPOOL-1")
    order.status = OrderStatus.OUT_FOR_DELIVERY
    db.session.commit()
    delivery = _create_delivery(
        db,
        order.id,
        delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.FAILED,
    )
    delivery.delivery_attempts = 1
    delivery.failed_delivery_reason = "customer_unavailable"
    db.session.commit()

    returned = StaffService.return_delivery_to_pool(delivery.id, admin_user.id, reason="retry")

    db.session.refresh(returned)
    db.session.refresh(order)
    assert returned.delivery_person_id is None
    assert returned.status == DeliveryStatus.SCHEDULED
    assert returned.failed_delivery_reason is None
    assert returned.delivery_attempts == 1  # preserved
    assert order.status == OrderStatus.CONFIRMED  # restored to pool-eligible
    # It now satisfies the pool query (unassigned + scheduled/pending + order confirmed).
    pool_ids = {item.id for item in StaffService.get_delivery_pool()["items"]}
    assert delivery.id in pool_ids


def test_admin_return_clears_driver_assignment(db, sample_user, delivery_driver, admin_user):
    """Admin moving a delivery to RETURNED must release the driver so the row
    cannot become a stranded pool-status delivery that still has a driver."""
    _create_delivery_person(db, delivery_driver, current_active_deliveries=1)
    order = _create_order(db, sample_user.id, "ORD-RETURNPOOL-2")
    order.status = OrderStatus.OUT_FOR_DELIVERY
    order.payment_method = PaymentMethod.CARD  # skip the cash-release branch; focus on driver clearing
    db.session.commit()
    delivery = _create_delivery(
        db,
        order.id,
        delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.IN_TRANSIT,
    )

    AdminDeliveryService._apply_status_update(
        delivery=delivery,
        new_status=DeliveryStatus.RETURNED,
        actor_id=admin_user.id,
        notes=None,
        fail_reason=None,
        cash_collected=None,
    )

    db.session.refresh(delivery)
    assert delivery.status == DeliveryStatus.RETURNED
    assert delivery.delivery_person_id is None


def test_assert_unassigned_for_pool_status_rejects_assigned_pool_delivery(db, sample_user, delivery_driver):
    """The pool invariant rejects a scheduled/pending delivery that still has a
    driver, and permits unassigned pool rows and non-pool statuses."""
    order = _create_order(db, sample_user.id, "ORD-INVARIANT-1")
    delivery = _create_delivery(
        db, order.id, delivery_person_id=delivery_driver.id, status=DeliveryStatus.SCHEDULED
    )

    with pytest.raises(InvalidStateTransition):
        assert_unassigned_for_pool_status(delivery, DeliveryStatus.SCHEDULED)

    # Non-pool target status is not constrained.
    assert_unassigned_for_pool_status(delivery, DeliveryStatus.ASSIGNED)

    # Unassigned pool row is fine.
    delivery.delivery_person_id = None
    assert_unassigned_for_pool_status(delivery, DeliveryStatus.SCHEDULED)


def test_redispatch_failed_delivery_returns_it_to_pool(db, sample_user, delivery_driver, admin_user):
    """Re-dispatching a FAILED delivery clears the driver, resets to SCHEDULED,
    restores the order to pool-eligible, and surfaces it in the pool."""
    _create_delivery_person(db, delivery_driver, current_active_deliveries=1)
    order = _create_order(db, sample_user.id, "ORD-REDISPATCH-1")
    order.status = OrderStatus.OUT_FOR_DELIVERY
    db.session.commit()
    delivery = _create_delivery(
        db,
        order.id,
        delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.FAILED,
    )

    StaffService.redispatch_failed_delivery(delivery.id, admin_user.id, reason="retry after failure")

    db.session.refresh(delivery)
    db.session.refresh(order)
    assert delivery.status == DeliveryStatus.SCHEDULED
    assert delivery.delivery_person_id is None
    assert order.status == OrderStatus.CONFIRMED
    pool_ids = {item.id for item in StaffService.get_delivery_pool()["items"]}
    assert delivery.id in pool_ids


def test_redispatch_rejects_non_failed_delivery(db, sample_user, delivery_driver, admin_user):
    """Only FAILED deliveries can be re-dispatched."""
    order = _create_order(db, sample_user.id, "ORD-REDISPATCH-2")
    delivery = _create_delivery(
        db, order.id, delivery_person_id=delivery_driver.id, status=DeliveryStatus.IN_TRANSIT
    )

    with pytest.raises(ValidationError) as exc_info:
        StaffService.redispatch_failed_delivery(delivery.id, admin_user.id)
    assert exc_info.value.error_code == "STAFF_DELIVERY_NOT_REDISPATCHABLE"

    db.session.refresh(delivery)
    assert delivery.status == DeliveryStatus.IN_TRANSIT  # unchanged


def test_get_failed_deliveries_lists_only_failed(db, sample_user, delivery_driver):
    """get_failed_deliveries returns failed rows (for the operator pick list) and
    excludes non-failed ones."""
    failed_order = _create_order(db, sample_user.id, "ORD-FAILEDLIST-1")
    failed = _create_delivery(
        db, failed_order.id, delivery_person_id=delivery_driver.id, status=DeliveryStatus.FAILED
    )
    active_order = _create_order(db, sample_user.id, "ORD-FAILEDLIST-2")
    _create_delivery(
        db, active_order.id, delivery_person_id=delivery_driver.id, status=DeliveryStatus.IN_TRANSIT
    )

    ids = {d.id for d in StaffService.get_failed_deliveries()}
    assert failed.id in ids
    assert len(ids) == 1


def test_monitor_stranded_deliveries_flags_only_assigned_pool_rows(db, sample_user, delivery_driver):
    """The monitoring task counts deliveries in a pool status that still have a
    driver, and ignores healthy rows (unassigned pool rows, assigned actives)."""
    from business_app.tasks.delivery_monitoring_tasks import monitor_stranded_deliveries

    stranded_order = _create_order(db, sample_user.id, "ORD-STRANDED-1")
    stranded = _create_delivery(
        db, stranded_order.id, delivery_person_id=delivery_driver.id, status=DeliveryStatus.SCHEDULED
    )
    healthy_pool_order = _create_order(db, sample_user.id, "ORD-STRANDED-2")
    _create_delivery(db, healthy_pool_order.id, status=DeliveryStatus.SCHEDULED)  # unassigned: fine
    active_order = _create_order(db, sample_user.id, "ORD-STRANDED-3")
    _create_delivery(
        db, active_order.id, delivery_person_id=delivery_driver.id, status=DeliveryStatus.ASSIGNED
    )  # assigned active: fine

    result = monitor_stranded_deliveries()

    assert result["stranded_count"] == 1
    assert result["delivery_ids"] == [stranded.id]


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
