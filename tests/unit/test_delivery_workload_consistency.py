"""Regression tests for driver workload consistency across admin and staff flows."""

from datetime import UTC, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order, OrderStatusHistory
from business_app.models.user import User
from business_app.services.admin_delivery_service import AdminDeliveryService
from business_app.services.staff_service import StaffService
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, UserRole, UserStatus, UserType
from business_app.utils.exceptions import ConflictError, InvalidStateTransition, ValidationError
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


@pytest.fixture
def after_todays_release(monkeypatch):
    """Pin the business clock to 12:00 Tashkent today, after every rostered shift start.

    Re-dispatch is a reschedule to local today (R18 of
    docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md), and the clock
    decides where it lands: `scheduled` once today's release has passed, `rescheduled`
    before it. Three seams read that clock: `delivery_window.local_now` (the "today" it
    dates to), `local_windows.local_now` (it binds its own copy) and
    `order_schedule_service.get_utc_now` (the `release_at <= now` test).
    """
    from business_app.services import order_schedule_service
    from business_app.utils import delivery_window, local_windows

    local_noon = datetime.combine(delivery_window.local_now().date(), time(12, 0), tzinfo=ZoneInfo(DISPLAY_TIMEZONE))
    monkeypatch.setattr(delivery_window, "local_now", lambda: local_noon)
    monkeypatch.setattr(local_windows, "local_now", lambda: local_noon)
    monkeypatch.setattr(order_schedule_service, "get_utc_now", lambda: local_noon.astimezone(UTC))
    return local_noon


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


def test_redispatch_failed_delivery_returns_it_to_pool(
    db, sample_user, delivery_driver, admin_user, after_todays_release
):
    """Re-dispatching a FAILED delivery is a reschedule to local today (R18). After
    today's release it clears the driver, lands SCHEDULED, restores the order to
    pool-eligible, dates it today, and surfaces it in the pool."""
    profile = _create_delivery_person(db, delivery_driver, current_active_deliveries=1)
    # The release instant is today + the earliest rostered shift start, so it is set
    # here rather than left to the model default.
    profile.working_hours_start = "08:00"
    db.session.commit()
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
    assert order.delivery_date == after_todays_release.date()
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


def test_return_delivery_to_pool_rejects_order_cancelled_independently(
    db, sample_user, delivery_driver, admin_user
):
    """A delivery can sit FAILED for months after its order is cancelled through
    a completely separate flow — the two are not kept in lockstep once the
    delivery reaches a terminal status. Returning such a delivery to the pool
    must not resurrect the dead order. This is the exact mechanism behind the
    prod incident where TG_000116_26 and TG_000280_26 reappeared as
    'confirmed' months after being cancelled."""
    order = _create_order(db, sample_user.id, "ORD-DEADORDER-1")
    order.status = OrderStatus.CANCELLED
    db.session.commit()
    delivery = _create_delivery(
        db, order.id, delivery_person_id=delivery_driver.id, status=DeliveryStatus.FAILED
    )

    with pytest.raises(ConflictError) as exc_info:
        StaffService.return_delivery_to_pool(delivery.id, admin_user.id, reason="retry")
    assert exc_info.value.error_code == "STAFF_ORDER_NOT_ACTIVE"

    db.session.refresh(delivery)
    db.session.refresh(order)
    assert delivery.status == DeliveryStatus.FAILED  # unchanged
    assert delivery.delivery_person_id == delivery_driver.id  # unchanged
    assert order.status == OrderStatus.CANCELLED  # unchanged — never resurrected


def test_redispatch_rejects_delivery_whose_order_was_cancelled_independently(
    db, sample_user, delivery_driver, admin_user
):
    """Same guard via the redispatch_failed_delivery entry point (staff bot /
    admin panel 'failed deliveries' list). Re-dispatch is a reschedule now, so the
    refusal is the reschedule SSOT's own: 400 ORDER_NOT_RESCHEDULABLE (R1),
    replacing the old 409 STAFF_ORDER_NOT_ACTIVE."""
    order = _create_order(db, sample_user.id, "ORD-DEADORDER-2")
    order.status = OrderStatus.CANCELLED
    db.session.commit()
    delivery = _create_delivery(
        db, order.id, delivery_person_id=delivery_driver.id, status=DeliveryStatus.FAILED
    )

    with pytest.raises(ValidationError) as exc_info:
        StaffService.redispatch_failed_delivery(delivery.id, admin_user.id)
    assert exc_info.value.error_code == "ORDER_NOT_RESCHEDULABLE"

    db.session.refresh(delivery)
    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.delivery_person_id == delivery_driver.id


def test_get_failed_deliveries_excludes_orders_no_longer_active(db, sample_user, delivery_driver):
    """A FAILED delivery whose order was independently cancelled (or delivered
    / returned) must never surface as a redispatch candidate — see the prod
    incident this guards against."""
    dead_order = _create_order(db, sample_user.id, "ORD-FAILEDLIST-DEAD")
    dead_order.status = OrderStatus.CANCELLED
    db.session.commit()
    dead = _create_delivery(
        db, dead_order.id, delivery_person_id=delivery_driver.id, status=DeliveryStatus.FAILED
    )
    live_order = _create_order(db, sample_user.id, "ORD-FAILEDLIST-LIVE")
    live_order.status = OrderStatus.OUT_FOR_DELIVERY
    db.session.commit()
    live = _create_delivery(
        db, live_order.id, delivery_person_id=delivery_driver.id, status=DeliveryStatus.FAILED
    )

    ids = {d.id for d in StaffService.get_failed_deliveries()}
    assert live.id in ids
    assert dead.id not in ids


def test_redispatch_failed_delivery_logs_order_status_history(db, sample_user, delivery_driver, admin_user):
    """The order-status restore (OUT_FOR_DELIVERY -> CONFIRMED) must leave an
    audit trail via the OrderStatusHistory SSOT. Before the fix this went
    through a raw `order.status =` write with no history row at all — exactly
    why the prod resurrection of TG_000116_26 / TG_000280_26 had no recorded
    transition and looked inexplicable."""
    order = _create_order(db, sample_user.id, "ORD-REDISPATCH-HISTORY")
    order.status = OrderStatus.OUT_FOR_DELIVERY
    db.session.commit()
    delivery = _create_delivery(
        db, order.id, delivery_person_id=delivery_driver.id, status=DeliveryStatus.FAILED
    )

    StaffService.redispatch_failed_delivery(delivery.id, admin_user.id, reason="retry after failure")

    db.session.refresh(order)
    assert order.status == OrderStatus.CONFIRMED
    history = (
        OrderStatusHistory.query.filter_by(order_id=order.id).order_by(OrderStatusHistory.id.desc()).first()
    )
    assert history is not None
    assert history.old_status == OrderStatus.OUT_FOR_DELIVERY
    assert history.new_status == OrderStatus.CONFIRMED
    assert history.changed_by == admin_user.id


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


def test_monitor_stranded_deliveries_flags_a_held_row_that_kept_its_driver(db, sample_user, delivery_driver):
    """A `rescheduled` row is driverless by definition (DELIVERY_DRIVERLESS_STATES), so one that
    kept a driver is stranded exactly like a pool row. SQLite has no CHECK, which is the only
    reason this test can build the row Postgres refuses."""
    from business_app.tasks.delivery_monitoring_tasks import monitor_stranded_deliveries

    held_order = _create_order(db, sample_user.id, "ORD-STRANDED-HELD-1")
    held = _create_delivery(
        db, held_order.id, delivery_person_id=delivery_driver.id, status=DeliveryStatus.RESCHEDULED
    )
    healthy_held_order = _create_order(db, sample_user.id, "ORD-STRANDED-HELD-2")
    _create_delivery(db, healthy_held_order.id, status=DeliveryStatus.RESCHEDULED)  # driverless: fine

    result = monitor_stranded_deliveries()

    assert result == {"stranded_count": 1, "delivery_ids": [held.id]}


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


# --------------------------------------------------------------------------- #
# `_pull_back_delivery`: the core shared by "return to pool" and the admin
# reschedule (docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md 3.3)
# --------------------------------------------------------------------------- #


def test_pull_back_parks_a_delivery_driverless_and_leaves_the_commit_to_the_caller(
    db, sample_user, delivery_driver, admin_user
):
    """The reschedule runs this core inside its own transaction, beside the new
    dates, so the core writes and never commits. A rollback must undo all of it."""
    from business_app.models.delivery import DeliveryStatusHistory

    _create_delivery_person(db, delivery_driver, current_active_deliveries=1)
    order = _create_order(db, sample_user.id, "ORD-PULLBACK-1")
    order.status = OrderStatus.OUT_FOR_DELIVERY
    db.session.commit()
    delivery = _create_delivery(
        db, order.id, delivery_person_id=delivery_driver.id, status=DeliveryStatus.IN_TRANSIT
    )
    driver_id, admin_id = delivery_driver.id, admin_user.id

    result = StaffService._pull_back_delivery(
        delivery,
        admin_id,
        target_status=DeliveryStatus.RESCHEDULED,
        reason="customer travelling",
        notes="Rescheduled to 2026-09-25",
    )

    assert result == (DeliveryStatus.IN_TRANSIT, driver_id)
    assert (delivery.status, delivery.delivery_person_id) == (DeliveryStatus.RESCHEDULED, None)
    assert order.status == OrderStatus.CONFIRMED
    (moved,) = DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id).all()
    assert (moved.old_status, moved.new_status, moved.changed_by, moved.reason, moved.notes) == (
        DeliveryStatus.IN_TRANSIT,
        DeliveryStatus.RESCHEDULED,
        admin_id,
        "customer travelling",
        "Rescheduled to 2026-09-25",
    )
    (restored,) = OrderStatusHistory.query.filter_by(order_id=order.id).all()
    assert (restored.old_status, restored.new_status, restored.notes) == (
        OrderStatus.OUT_FOR_DELIVERY,
        OrderStatus.CONFIRMED,
        None,
    )
    assert DeliveryPerson.query.filter_by(user_id=driver_id).one().current_active_deliveries == 0

    db.session.rollback()

    assert (delivery.status, delivery.delivery_person_id) == (DeliveryStatus.IN_TRANSIT, driver_id)
    assert order.status == OrderStatus.OUT_FOR_DELIVERY
    assert DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id).count() == 0
    assert OrderStatusHistory.query.filter_by(order_id=order.id).count() == 0


def test_pull_back_of_a_row_already_held_records_no_transition(db, sample_user, admin_user):
    """Re-dating a delivery that is already waiting for its day is not a status
    change: no DeliveryStatusHistory row, and the CONFIRMED order is left alone."""
    from business_app.models.delivery import DeliveryStatusHistory

    order = _create_order(db, sample_user.id, "ORD-PULLBACK-2")
    delivery = _create_delivery(db, order.id, status=DeliveryStatus.RESCHEDULED)

    result = StaffService._pull_back_delivery(
        delivery,
        admin_user.id,
        target_status=DeliveryStatus.RESCHEDULED,
        reason="moved again",
        notes="Rescheduled to 2026-09-26",
    )

    assert result == (DeliveryStatus.RESCHEDULED, None)
    assert DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id).count() == 0
    assert OrderStatusHistory.query.filter_by(order_id=order.id).count() == 0
    assert order.status == OrderStatus.CONFIRMED


def test_pull_back_with_restore_order_off_leaves_a_pending_order_untouched(db, sample_user, admin_user):
    """R23: a reschedule never confirms an order. With the default the core would
    take this PENDING (unpaid) order to CONFIRMED through `update_order_status`,
    which runs the CONFIRMED side effects (inventory confirmation, the release
    gate). `restore_order=False` leaves the order alone and still parks the
    delivery."""
    from business_app.models.delivery import DeliveryStatusHistory

    order = _create_order(db, sample_user.id, "ORD-PULLBACK-PENDING")
    order.status = OrderStatus.PENDING
    db.session.commit()
    delivery = _create_delivery(db, order.id, status=DeliveryStatus.SCHEDULED)

    result = StaffService._pull_back_delivery(
        delivery,
        admin_user.id,
        target_status=DeliveryStatus.RESCHEDULED,
        reason="customer travelling",
        notes="Rescheduled to 2026-09-25",
        restore_order=False,
    )

    assert result == (DeliveryStatus.SCHEDULED, None)
    assert (delivery.status, delivery.delivery_person_id) == (DeliveryStatus.RESCHEDULED, None)
    assert order.status == OrderStatus.PENDING
    assert OrderStatusHistory.query.filter_by(order_id=order.id).count() == 0
    (moved,) = DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id).all()
    assert (moved.old_status, moved.new_status, moved.changed_by, moved.reason) == (
        DeliveryStatus.SCHEDULED,
        DeliveryStatus.RESCHEDULED,
        admin_user.id,
        "customer travelling",
    )


@pytest.mark.parametrize(
    "target", [DeliveryStatus.PENDING, DeliveryStatus.ASSIGNED, DeliveryStatus.FAILED], ids=lambda s: s.value
)
def test_pull_back_parks_only_in_scheduled_or_rescheduled(db, sample_user, delivery_driver, admin_user, target):
    order = _create_order(db, sample_user.id, f"ORD-PULLBACK-{target.value}")
    delivery = _create_delivery(
        db, order.id, delivery_person_id=delivery_driver.id, status=DeliveryStatus.ASSIGNED
    )

    with pytest.raises(ValueError):
        StaffService._pull_back_delivery(delivery, admin_user.id, target_status=target)

    assert (delivery.status, delivery.delivery_person_id) == (DeliveryStatus.ASSIGNED, delivery_driver.id)


def test_return_to_pool_judges_the_locked_row_not_a_stale_copy(db, sample_user, delivery_driver, admin_user):
    """`RouteEditService.return_stop_to_pool` loads the delivery unlocked before it
    calls this, and an admin may have rescheduled it in between. `with_for_update()`
    alone hands back the stale ASSIGNED instance, and the held stop would be
    dragged into today's pool."""
    from sqlalchemy import update

    from business_app.models.delivery import DeliveryStatusHistory

    order = _create_order(db, sample_user.id, "ORD-STALEPOOL-1")
    delivery = _create_delivery(
        db, order.id, delivery_person_id=delivery_driver.id, status=DeliveryStatus.ASSIGNED
    )
    assert delivery.status == DeliveryStatus.ASSIGNED  # loaded into this session
    # The reschedule commits from another request: the row moves, this
    # session's instance does not.
    db.session.execute(
        update(Delivery)
        .where(Delivery.id == delivery.id)
        .values(status=DeliveryStatus.RESCHEDULED, delivery_person_id=None),
        execution_options={"synchronize_session": False},
    )

    with pytest.raises(ConflictError) as exc_info:
        StaffService.return_delivery_to_pool(delivery.id, admin_user.id, reason="stale board")

    assert exc_info.value.error_code == "STAFF_DELIVERY_NOT_POOLABLE"
    assert (delivery.status, delivery.delivery_person_id) == (DeliveryStatus.RESCHEDULED, None)
    assert DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id).count() == 0
