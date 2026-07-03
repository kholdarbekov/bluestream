"""Unit tests for the unified bottle session integration paths.

Covers:
- StaffService delivery → DriverBottleSession tally + bind_order_to_session
- BottleTrackingService.record_standalone_collection → session tally
- TryoutService.record_pickup → session tally
- Co-driver membership lifecycle
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch, call

import pytest

from business_app.models.bottle import (
    DriverBottleSession,
    DriverBottleSessionOrder,
    DriverSessionMembership,
)
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import (
    DriverBottleSessionStatus,
    DriverSessionMembershipStatus,
    UserRole,
    UserType,
)
from business_app.utils.exceptions import ConflictError, ValidationError
from business_app.utils.password_security import hash_password


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_driver(db, phone: str, first_name: str = "Driver"):
    from business_app.models.user import User
    from datetime import datetime, UTC
    user = User(
        phone=phone,
        first_name=first_name,
        last_name="Test",
        password_hash=hash_password("TestPassword123!"),
        role=UserRole.DELIVERY_DRIVER,
        user_type=UserType.STAFF,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.flush()
    return user


def _open_session(db, driver) -> DriverBottleSession:
    session = DriverBottleSession(
        driver_user_id=driver.id,
        bottles_loaded=20,
        status=DriverBottleSessionStatus.OPEN,
    )
    db.session.add(session)
    db.session.flush()
    return session


# ---------------------------------------------------------------------------
# BottleTrackingService.update_session_delivery_tally
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_update_session_tally_increments_counts(db):
    """update_session_delivery_tally increments both delivered and collected."""
    driver = _make_driver(db, "+998901000001")
    session = _open_session(db, driver)

    svc = BottleTrackingService()
    result = svc.update_session_delivery_tally(
        driver.id,
        bottles_delivered=3,
        bottles_collected=1,
    )

    db.session.refresh(session)
    assert result is not None
    assert session.bottles_delivered == 3
    assert session.bottles_collected_from_customers == 1


@pytest.mark.unit
def test_update_session_tally_no_session_is_noop(db):
    """update_session_delivery_tally silently returns None when driver has no session."""
    driver = _make_driver(db, "+998901000002")

    svc = BottleTrackingService()
    result = svc.update_session_delivery_tally(
        driver.id,
        bottles_delivered=5,
        bottles_collected=2,
    )

    assert result is None


@pytest.mark.unit
def test_update_session_tally_accumulates_across_calls(db):
    """Multiple tally calls accumulate, not overwrite."""
    driver = _make_driver(db, "+998901000003")
    session = _open_session(db, driver)

    svc = BottleTrackingService()
    svc.update_session_delivery_tally(driver.id, bottles_delivered=2, bottles_collected=0)
    svc.update_session_delivery_tally(driver.id, bottles_delivered=1, bottles_collected=1)

    db.session.refresh(session)
    assert session.bottles_delivered == 3
    assert session.bottles_collected_from_customers == 1


# ---------------------------------------------------------------------------
# BottleTrackingService.record_standalone_collection → session tally
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_standalone_collection_updates_session_tally(db, sample_user):
    """record_standalone_collection increments bottles_collected_from_customers."""
    driver = _make_driver(db, "+998901000004")
    session = _open_session(db, driver)

    # Create an address for the customer
    from business_app.models.user import UserAddress
    address = UserAddress(
        user_id=sample_user.id,
        title="Home",
        full_address="123 Test St",
        city="Tashkent",
    )
    db.session.add(address)
    db.session.flush()

    # Pre-seed a positive balance so the collection doesn't go below zero
    from business_app.models.bottle import BottleBalance
    balance = BottleBalance(
        user_id=sample_user.id,
        address_id=address.id,
        balance=Decimal("5.00"),
    )
    db.session.add(balance)
    db.session.flush()

    svc = BottleTrackingService()
    svc.record_standalone_collection(
        user_id=sample_user.id,
        address_id=address.id,
        quantity=Decimal("3"),
        actor_user_id=driver.id,
    )

    db.session.refresh(session)
    assert session.bottles_collected_from_customers == 3


@pytest.mark.unit
def test_standalone_collection_no_session_is_noop(db, sample_user):
    """record_standalone_collection doesn't fail when driver has no open session."""
    driver = _make_driver(db, "+998901000005")

    from business_app.models.user import UserAddress
    address = UserAddress(
        user_id=sample_user.id,
        title="Home",
        full_address="123 Test St",
        city="Tashkent",
    )
    db.session.add(address)
    db.session.flush()

    from business_app.models.bottle import BottleBalance
    balance = BottleBalance(
        user_id=sample_user.id,
        address_id=address.id,
        balance=Decimal("5.00"),
    )
    db.session.add(balance)
    db.session.flush()

    svc = BottleTrackingService()
    # Should not raise
    entry = svc.record_standalone_collection(
        user_id=sample_user.id,
        address_id=address.id,
        quantity=Decimal("2"),
        actor_user_id=driver.id,
    )
    assert entry is not None


# ---------------------------------------------------------------------------
# BottleTrackingService.bind_order_to_session
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_bind_order_to_session_creates_record(db, sample_user):
    """bind_order_to_session creates a DriverBottleSessionOrder with correct fields."""
    from business_app.models.order import Order
    from shared.enums import OrderStatus
    driver = _make_driver(db, "+998901000006")
    session = _open_session(db, driver)

    order = Order(
        user_id=sample_user.id,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("50000"),
        total_amount=Decimal("50000"),
        order_source="phone",
    )
    db.session.add(order)
    db.session.flush()

    svc = BottleTrackingService()
    binding = svc.bind_order_to_session(
        session.id,
        order.id,
        accepted_by_driver_id=driver.id,
    )

    assert binding.session_id == session.id
    assert binding.order_id == order.id
    assert binding.accepted_by_driver_id == driver.id


@pytest.mark.unit
def test_bind_order_to_session_is_idempotent(db, sample_user):
    """Calling bind_order_to_session twice for the same order is safe."""
    from business_app.models.order import Order
    from shared.enums import OrderStatus
    driver = _make_driver(db, "+998901000007")
    session = _open_session(db, driver)

    order = Order(
        user_id=sample_user.id,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("50000"),
        total_amount=Decimal("50000"),
        order_source="phone",
    )
    db.session.add(order)
    db.session.flush()

    svc = BottleTrackingService()
    b1 = svc.bind_order_to_session(session.id, order.id, accepted_by_driver_id=driver.id)
    b2 = svc.bind_order_to_session(session.id, order.id, accepted_by_driver_id=driver.id)

    assert b1.id == b2.id
    count = DriverBottleSessionOrder.query.filter_by(order_id=order.id).count()
    assert count == 1


# ---------------------------------------------------------------------------
# StaffService delivery completion → session tally (via mock)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_staff_service_delivery_tallies_bound_session(
    db, sample_user, sample_product, monkeypatch
):
    """
    When a delivery is marked 'delivered', OrderService must credit the
    session the order was bound to at accept time (session continuity).
    The binding is now created in accept_order, not the DELIVERED handler.
    """
    from datetime import datetime, UTC, timedelta
    from business_app.models.delivery import Delivery
    from business_app.models.order import Order, OrderItem
    from business_app.models.user import UserAddress
    from business_app.services.staff_service import StaffService
    from shared.enums import OrderStatus

    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    db.session.flush()

    # Create driver + open session
    driver = _make_driver(db, "+998901000008", "DeliveryGuy")
    session = _open_session(db, driver)
    session.bottles_loaded = 10
    db.session.flush()

    address = UserAddress(
        user_id=sample_user.id,
        title="Home",
        full_address="123 Main St",
        city="Tashkent",
    )
    db.session.add(address)
    db.session.flush()

    # Create order + order item (2 units → 2 bottles expected in tally)
    order = Order(
        user_id=sample_user.id,
        status=OrderStatus.OUT_FOR_DELIVERY,
        subtotal=Decimal("30000"),
        total_amount=Decimal("30000"),
        order_source="phone",
        delivery_address_id=address.id,
    )
    db.session.add(order)
    db.session.flush()

    order_item = OrderItem(
        order_id=order.id,
        product_id=sample_product.id,
        quantity=2,
        unit_price=Decimal("15000"),
        total_price=Decimal("30000"),
    )
    db.session.add(order_item)
    db.session.flush()

    from shared.enums import DeliveryStatus
    delivery = Delivery(
        order_id=order.id,
        status=DeliveryStatus.ARRIVED,  # valid pre-condition for 'delivered' transition
        delivery_person_id=driver.id,
        scheduled_date=datetime.now(UTC) + timedelta(hours=1),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.flush()

    # Simulate accept: bind the order to the session up front (this is now
    # what accept_order does — the DELIVERED handler relies on this binding).
    BottleTrackingService().bind_order_to_session(
        session.id, order.id, accepted_by_driver_id=driver.id
    )
    db.session.commit()

    # Skip the customer-ledger side effects to keep the test focused on
    # the session-tally invariant.
    def _noop_delivered(self, order_id, user_id, address_id, quantity, actor_user_id):
        return None

    def _noop_returned(self, **_kwargs):
        return None

    monkeypatch.setattr(BottleTrackingService, "record_bottles_delivered", _noop_delivered)
    monkeypatch.setattr(BottleTrackingService, "record_bottles_returned", _noop_returned)

    StaffService.update_delivery_status(
        delivery.id,
        new_status="delivered",
        staff_user_id=driver.id,
        metadata={"bottles_returned": 1},
    )

    db.session.refresh(session)
    # Tally credited to the bound session, not whichever session is open
    # at delivery time. 2 units × 1 bottle = 2 delivered, 1 returned.
    assert session.bottles_delivered == 2
    assert session.bottles_collected_from_customers == 1


# ---------------------------------------------------------------------------
# Tryout pickup → session tally
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_tryout_pickup_updates_session_tally(db, sample_product, admin_user):
    """record_pickup must call update_session_delivery_tally for bottles_collected."""
    sample_product.is_tryout_eligible = True
    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    sample_product.stock_quantity = 10
    db.session.commit()

    from business_app.services.tryout_service import TryoutService

    tryout = TryoutService.create_tryout(
        {
            "trial_contact": {
                "first_name": "Trial",
                "last_name": "Customer",
                "phone": "+998901112233",
                "preferred_language": "uz",
            },
            "address": {
                "label": "Office",
                "full_address": "12 Sample Street",
                "district": "Yunusabad",
                "city": "Tashkent",
                "is_default": True,
            },
            "items": [{"product_id": sample_product.id, "quantity": 3}],
            "complete_handoff": True,
        },
        admin_user.id,
        source="admin",
    )
    pickup_task = next(t for t in tryout.tasks if t.task_type.value == "pickup")

    # Create a driver with an open session to receive the tally
    driver = _make_driver(db, "+998901000009", "PickupDriver")
    session = _open_session(db, driver)

    tally_calls = []
    original_tally = BottleTrackingService.update_session_delivery_tally

    def patched_tally(self, driver_id, *, bottles_delivered=0, bottles_collected=0):
        tally_calls.append({
            "driver_id": driver_id,
            "bottles_collected": bottles_collected,
        })
        return original_tally(self, driver_id, bottles_delivered=bottles_delivered, bottles_collected=bottles_collected)

    with patch.object(BottleTrackingService, "update_session_delivery_tally", patched_tally):
        TryoutService.record_pickup(
            pickup_task.id,
            [{"product_id": sample_product.id, "units": Decimal("2.00")}],
            driver.id,
        )

    pickup_tallies = [c for c in tally_calls if c["driver_id"] == driver.id]
    assert len(pickup_tallies) >= 1
    assert pickup_tallies[0]["bottles_collected"] == 2

    db.session.refresh(session)
    assert session.bottles_collected_from_customers == 2


@pytest.mark.unit
def test_tryout_pickup_no_session_is_noop(db, sample_product, admin_user):
    """record_pickup must not raise when the actor driver has no open session."""
    sample_product.is_tryout_eligible = True
    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    sample_product.stock_quantity = 5
    db.session.commit()

    from business_app.services.tryout_service import TryoutService

    tryout = TryoutService.create_tryout(
        {
            "trial_contact": {
                "first_name": "Trial",
                "last_name": "Customer",
                "phone": "+998901112244",
                "preferred_language": "uz",
            },
            "address": {
                "label": "Home",
                "full_address": "99 No Session St",
                "district": "Chilonzor",
                "city": "Tashkent",
                "is_default": True,
            },
            "items": [{"product_id": sample_product.id, "quantity": 2}],
            "complete_handoff": True,
        },
        admin_user.id,
        source="admin",
    )
    pickup_task = next(t for t in tryout.tasks if t.task_type.value == "pickup")

    # Driver with NO open session
    driver = _make_driver(db, "+998901000010", "SessionlessDriver")

    # Should not raise
    tryout = TryoutService.record_pickup(
        pickup_task.id,
        [{"product_id": sample_product.id, "units": Decimal("1.00")}],
        driver.id,
    )
    assert tryout is not None


# ---------------------------------------------------------------------------
# Co-driver membership lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_codriver_join_session(db):
    """A co-driver can join an owner's open session."""
    owner = _make_driver(db, "+998901000011", "Owner")
    codriver = _make_driver(db, "+998901000012", "CoDriver")
    session = _open_session(db, owner)

    svc = BottleTrackingService()
    membership = svc.join_session(codriver.id, session.id)

    assert membership.member_driver_id == codriver.id
    assert membership.session_id == session.id
    assert membership.status == DriverSessionMembershipStatus.ACTIVE


@pytest.mark.unit
def test_codriver_cannot_join_own_session(db):
    """A driver cannot join their own session as co-driver."""
    owner = _make_driver(db, "+998901000013", "OwnerSelf")
    session = _open_session(db, owner)

    svc = BottleTrackingService()
    with pytest.raises(ValidationError):
        svc.join_session(owner.id, session.id)


@pytest.mark.unit
def test_codriver_cannot_double_join(db):
    """A co-driver already in an active membership cannot join another session."""
    owner1 = _make_driver(db, "+998901000014", "Owner1")
    owner2 = _make_driver(db, "+998901000015", "Owner2")
    codriver = _make_driver(db, "+998901000016", "CoDriverDouble")
    session1 = _open_session(db, owner1)
    session2 = _open_session(db, owner2)

    svc = BottleTrackingService()
    svc.join_session(codriver.id, session1.id)

    with pytest.raises(ConflictError):
        svc.join_session(codriver.id, session2.id)


@pytest.mark.unit
def test_get_effective_session_for_codriver(db):
    """get_effective_session returns the owner's session for a co-driver."""
    owner = _make_driver(db, "+998901000017", "OwnerEff")
    codriver = _make_driver(db, "+998901000018", "CoDriverEff")
    session = _open_session(db, owner)

    svc = BottleTrackingService()
    svc.join_session(codriver.id, session.id)

    effective = svc.get_effective_session(codriver.id)
    assert effective is not None
    assert effective.id == session.id


@pytest.mark.unit
def test_codriver_tally_updates_owner_session(db):
    """When a co-driver tallies, the owner's session counters increment."""
    owner = _make_driver(db, "+998901000019", "OwnerTally")
    codriver = _make_driver(db, "+998901000020", "CoDriverTally")
    session = _open_session(db, owner)

    svc = BottleTrackingService()
    svc.join_session(codriver.id, session.id)

    svc.update_session_delivery_tally(codriver.id, bottles_delivered=5, bottles_collected=2)

    db.session.refresh(session)
    assert session.bottles_delivered == 5
    assert session.bottles_collected_from_customers == 2


@pytest.mark.unit
def test_codriver_no_longer_sees_session_after_leave(db):
    """After leaving a session, the co-driver has no effective session."""
    owner = _make_driver(db, "+998901000021", "OwnerLeave")
    codriver = _make_driver(db, "+998901000022", "CoDriverLeave")
    session = _open_session(db, owner)

    svc = BottleTrackingService()
    membership = svc.join_session(codriver.id, session.id)

    # Simulate leave by marking membership as LEFT
    membership.status = DriverSessionMembershipStatus.LEFT
    db.session.flush()

    effective = svc.get_effective_session(codriver.id)
    assert effective is None


@pytest.mark.unit
def test_codriver_membership_revoked_when_owner_closes_session(db):
    """When the owner closes their session, all active co-driver memberships are revoked."""
    from shared.enums import DriverSessionMembershipStatus as MStatus
    owner = _make_driver(db, "+998901000023", "OwnerClose")
    codriver = _make_driver(db, "+998901000024", "CoDriverClose")
    session = _open_session(db, owner)
    session.bottles_loaded = 5
    db.session.flush()

    svc = BottleTrackingService()
    membership = svc.join_session(codriver.id, session.id)
    assert membership.status == MStatus.ACTIVE

    # Owner closes session
    svc.close_bottle_session(owner.id, bottles_returned_to_warehouse=5)
    db.session.flush()

    db.session.refresh(membership)
    assert membership.status == MStatus.REVOKED

    # Co-driver no longer has an effective session
    effective = svc.get_effective_session(codriver.id)
    assert effective is None


# ---------------------------------------------------------------------------
# assert_driver_can_progress_delivery — the new transition-guard
# ---------------------------------------------------------------------------

def _make_order_with_bottles(db, customer, product, *, quantity=2):
    """Create an order + order item that requires `quantity` returnable bottles."""
    from business_app.models.order import Order, OrderItem
    from business_app.models.user import UserAddress
    from shared.enums import OrderStatus

    product.tracks_returnable_bottles = True
    product.returnable_bottles_per_unit = Decimal("1.00")
    db.session.flush()

    address = UserAddress(
        user_id=customer.id,
        title="Home",
        full_address="123 Test St",
        city="Tashkent",
    )
    db.session.add(address)
    db.session.flush()

    order = Order(
        user_id=customer.id,
        status=OrderStatus.OUT_FOR_DELIVERY,
        subtotal=Decimal("10000"),
        total_amount=Decimal("10000"),
        order_source="phone",
        delivery_address_id=address.id,
    )
    db.session.add(order)
    db.session.flush()

    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=quantity,
            unit_price=Decimal("5000"),
            total_price=Decimal("5000") * quantity,
        )
    )
    db.session.flush()
    return order


def _make_delivery(db, order, driver):
    from datetime import datetime, UTC, timedelta
    from business_app.models.delivery import Delivery
    from shared.enums import DeliveryStatus

    delivery = Delivery(
        order_id=order.id,
        status=DeliveryStatus.ASSIGNED,
        delivery_person_id=driver.id,
        scheduled_date=datetime.now(UTC) + timedelta(hours=1),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.flush()
    return delivery


@pytest.mark.unit
def test_assert_progress_returns_none_for_order_without_bottles(db, sample_user, sample_product):
    """Orders with no returnable bottles bypass session checks entirely."""
    from business_app.models.order import Order, OrderItem
    from shared.enums import OrderStatus

    sample_product.tracks_returnable_bottles = False
    db.session.flush()

    driver = _make_driver(db, "+998901000100", "NoBottles")
    order = Order(
        user_id=sample_user.id,
        status=OrderStatus.OUT_FOR_DELIVERY,
        subtotal=Decimal("10000"),
        total_amount=Decimal("10000"),
        order_source="phone",
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=sample_product.id,
            quantity=1,
            unit_price=Decimal("10000"),
            total_price=Decimal("10000"),
        )
    )
    db.session.flush()

    delivery = _make_delivery(db, order, driver)

    svc = BottleTrackingService()
    assert svc.assert_driver_can_progress_delivery(delivery) is None


@pytest.mark.unit
def test_assert_progress_late_binds_when_no_binding_but_driver_has_open_session(
    db, app, sample_user, sample_product
):
    """No binding exists (order assigned via admin/auto-assign, which never bind)
    but the driver has an open session → late-bind the order onto that session
    instead of raising. Reproduces prod TG_000183_26 / delivery 620 where
    auto_assign_delivery_task assigned the order without a binding."""
    driver = _make_driver(db, "+998901000120", "LateBind")
    session = _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_delivery(db, order, driver)

    svc = BottleTrackingService()
    # No bind_order_to_session call — mirrors auto_assign_delivery_task.
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).first() is None

    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            got = svc.assert_driver_can_progress_delivery(delivery)
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False

    assert got is not None
    assert got.id == session.id
    binding = DriverBottleSessionOrder.query.filter_by(order_id=order.id).first()
    assert binding is not None
    assert binding.session_id == session.id


@pytest.mark.unit
def test_assert_progress_strict_raises_when_no_binding_and_no_open_session(
    db, app, sample_user, sample_product
):
    """Strict mode: missing binding AND the driver has no open session — there is
    nothing to bind onto, so BOTTLE_SESSION_REQUIRED (open a session first)."""
    driver = _make_driver(db, "+998901000101", "Strict1")
    # Intentionally NO open session.
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_delivery(db, order, driver)

    svc = BottleTrackingService()
    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            with pytest.raises(ValidationError) as exc:
                svc.assert_driver_can_progress_delivery(delivery)
            assert exc.value.error_code == "BOTTLE_SESSION_REQUIRED"
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False


@pytest.mark.unit
def test_return_to_pool_clears_binding_so_next_driver_can_accept(db, app, sample_user, sample_product):
    """return_delivery_to_pool removes the stale DriverBottleSessionOrder so a
    different driver re-accepting the redispatched delivery does not hit
    ConflictError ('already bound to session Y')."""
    from business_app.services.staff_service import StaffService
    driver_a = _make_driver(db, "+998901600001", "DriverA")
    session_a = _open_session(db, driver_a)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_delivery(db, order, driver_a)
    db.session.commit()

    svc = BottleTrackingService()
    svc.bind_order_to_session(session_a.id, order.id, accepted_by_driver_id=driver_a.id)
    db.session.commit()

    with app.test_request_context():
        StaffService.return_delivery_to_pool(delivery.id, actor_id=driver_a.id, reason="failed")

    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).first() is None


@pytest.mark.unit
def test_assert_progress_late_bind_respects_session_capacity(
    db, app, sample_user, sample_product
):
    """Late-bind enforces session capacity exactly like accept time: if the
    driver's open session can't cover the order's bottles, raise
    BOTTLE_SESSION_CAPACITY_EXCEEDED and create no binding."""
    driver = _make_driver(db, "+998901000123", "CapDriver")
    session = _open_session(db, driver)
    session.bottles_loaded = 1  # only 1 bottle available
    db.session.flush()
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=5)
    delivery = _make_delivery(db, order, driver)

    svc = BottleTrackingService()
    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            with pytest.raises(ValidationError) as exc:
                svc.assert_driver_can_progress_delivery(delivery)
            assert exc.value.error_code == "BOTTLE_SESSION_CAPACITY_EXCEEDED"
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False

    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).first() is None


@pytest.mark.unit
def test_assert_progress_legacy_late_binds_when_no_binding_and_open_session(
    db, app, sample_user, sample_product
):
    """Legacy mode mirrors strict for the (re)bind path: a never-bound order
    late-binds onto the driver's open session (no raise). Only the hard error for
    the no-open-session case is suppressed in legacy mode."""
    driver = _make_driver(db, "+998901000102", "Legacy1")
    session = _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_delivery(db, order, driver)

    svc = BottleTrackingService()
    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False
        result = svc.assert_driver_can_progress_delivery(delivery)

    assert result is not None
    assert result.id == session.id
    assert (
        DriverBottleSessionOrder.query.filter_by(order_id=order.id).first().session_id
        == session.id
    )


@pytest.mark.unit
def test_assert_progress_legacy_returns_none_when_no_binding_and_no_open_session(
    db, app, sample_user, sample_product
):
    """Legacy mode: missing binding and no open session → return None, no raise
    (keeps legacy/dev environments unblocked; strict mode raises here)."""
    driver = _make_driver(db, "+998901000122", "Legacy2")
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_delivery(db, order, driver)

    svc = BottleTrackingService()
    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False
        assert svc.assert_driver_can_progress_delivery(delivery) is None


@pytest.mark.unit
def test_assert_progress_strict_raises_when_session_closed_and_no_open_session(
    db, app, sample_user, sample_product
):
    """Strict mode: bound session is closed and the driver has NO open session
    to carry the order onto → BOTTLE_SESSION_REQUIRED (open a new one)."""
    driver = _make_driver(db, "+998901000103", "Strict2")
    session = _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_delivery(db, order, driver)

    svc = BottleTrackingService()
    svc.bind_order_to_session(session.id, order.id, accepted_by_driver_id=driver.id)

    # Close the driver's only session out from under the bound order.
    session.status = DriverBottleSessionStatus.CLOSED
    db.session.flush()

    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            with pytest.raises(ValidationError) as exc:
                svc.assert_driver_can_progress_delivery(delivery)
            assert exc.value.error_code == "BOTTLE_SESSION_REQUIRED"
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False


@pytest.mark.unit
def test_assert_progress_migrates_to_drivers_own_open_session(
    db, app, sample_user, sample_product
):
    """Carry-over: when the order is bound to a session other than the delivery
    driver's current open session, the binding migrates onto the driver's own
    open session (capacity permitting) instead of raising a mismatch.
    """
    driver_a = _make_driver(db, "+998901000104", "DriverA")
    driver_b = _make_driver(db, "+998901000114", "DriverB")
    session_a = _open_session(db, driver_a)
    session_b = _open_session(db, driver_b)

    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_delivery(db, order, driver_a)

    svc = BottleTrackingService()
    # Order is bound to driver B's session even though delivery is assigned to
    # driver A. Driver A has their own open session, so progressing the delivery
    # migrates the binding onto A's session.
    svc.bind_order_to_session(session_b.id, order.id, accepted_by_driver_id=driver_b.id)
    db.session.flush()

    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            got = svc.assert_driver_can_progress_delivery(delivery)
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False

    assert got is not None
    assert got.id == session_a.id
    binding = DriverBottleSessionOrder.query.filter_by(order_id=order.id).first()
    assert binding.session_id == session_a.id
    assert binding.accepted_by_driver_id == driver_a.id


def _make_driver_profile(db, driver, phone):
    """Attach an active DeliveryPerson profile so assign_delivery_driver accepts
    the driver (driver identity is the profile, not User.role)."""
    from business_app.models.delivery import DeliveryPerson

    profile = DeliveryPerson(
        user_id=driver.id,
        full_name="AssignDriver",
        phone=phone,
        is_active=True,
        is_available=True,
        working_hours_start="00:00",
        working_hours_end="23:59",
    )
    db.session.add(profile)
    db.session.flush()
    return profile


def _make_scheduled_unassigned_delivery(db, order):
    """A SCHEDULED delivery with no driver — the auto-assign / admin entrypoint."""
    from datetime import datetime, UTC, timedelta
    from business_app.models.delivery import Delivery
    from shared.enums import DeliveryStatus

    delivery = Delivery(
        order_id=order.id,
        status=DeliveryStatus.SCHEDULED,
        scheduled_date=datetime.now(UTC) + timedelta(hours=1),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.flush()
    return delivery


@pytest.mark.unit
def test_assign_delivery_driver_binds_order_to_open_session(db, app, sample_user, sample_product):
    """DeliveryService.assign_delivery_driver (admin / auto_assign_delivery_task)
    binds the order to the driver's open bottle session when one exists, mirroring
    the bot-accept flow so non-bot assignment no longer leaves the order unbound
    (the root cause of prod TG_000183_26 / delivery 620)."""
    from business_app.models.delivery import Delivery, DeliveryPerson
    from business_app.services.delivery_service import DeliveryService

    driver = _make_driver(db, "+998901000130", "AssignBind")
    _make_driver_profile(db, driver, "+998901000130")
    session = _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_scheduled_unassigned_delivery(db, order)
    db.session.commit()

    with app.test_request_context(), patch.object(
        DeliveryService, "_notify_driver", lambda self, d: None
    ), patch.object(DeliveryService, "_optimize_driver_route", lambda self, *a, **k: None):
        DeliveryService().assign_delivery_driver(delivery.id, driver.id)

    refreshed = Delivery.query.get(delivery.id)
    assert refreshed.delivery_person_id == driver.id
    binding = DriverBottleSessionOrder.query.filter_by(order_id=order.id).first()
    assert binding is not None
    assert binding.session_id == session.id


@pytest.mark.unit
def test_assign_delivery_driver_skips_binding_without_open_session(db, app, sample_user, sample_product):
    """Best-effort: with no open session at assignment time, assignment still
    succeeds and creates no binding — the progress guard late-binds later when the
    driver opens a session."""
    from business_app.models.delivery import Delivery
    from business_app.services.delivery_service import DeliveryService

    driver = _make_driver(db, "+998901000131", "AssignNoSession")
    _make_driver_profile(db, driver, "+998901000131")
    # No open session.
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_scheduled_unassigned_delivery(db, order)
    db.session.commit()

    with app.test_request_context(), patch.object(
        DeliveryService, "_notify_driver", lambda self, d: None
    ), patch.object(DeliveryService, "_optimize_driver_route", lambda self, *a, **k: None):
        DeliveryService().assign_delivery_driver(delivery.id, driver.id)

    refreshed = Delivery.query.get(delivery.id)
    assert refreshed.delivery_person_id == driver.id
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).first() is None


@pytest.mark.unit
def test_assert_progress_happy_path_returns_bound_session(
    db, app, sample_user, sample_product
):
    """Happy path: returns the bound session when everything lines up."""
    driver = _make_driver(db, "+998901000105", "Happy")
    session = _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_delivery(db, order, driver)

    svc = BottleTrackingService()
    svc.bind_order_to_session(session.id, order.id, accepted_by_driver_id=driver.id)

    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            got = svc.assert_driver_can_progress_delivery(delivery)
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False
    assert got is not None
    assert got.id == session.id


# ---------------------------------------------------------------------------
# Carry-over: orders live across sessions (incident TG_000132_26)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_assert_progress_carry_over_migrates_to_new_open_session(
    db, app, sample_user, sample_product
):
    """Incident shape: order accepted under session A, A is force-closed, driver
    opens session B, then progresses the delivery → binding migrates A→B and the
    guard returns B."""
    driver = _make_driver(db, "+998901000106", "CarryOver")
    session_a = _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_delivery(db, order, driver)

    svc = BottleTrackingService()
    svc.bind_order_to_session(session_a.id, order.id, accepted_by_driver_id=driver.id)

    # Session A is force-closed; the driver loads a fresh session B.
    session_a.status = DriverBottleSessionStatus.FORCE_CLOSED
    db.session.flush()
    session_b = _open_session(db, driver)

    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            got = svc.assert_driver_can_progress_delivery(delivery)
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False

    assert got is not None
    assert got.id == session_b.id
    binding = DriverBottleSessionOrder.query.filter_by(order_id=order.id).first()
    assert binding.session_id == session_b.id


@pytest.mark.unit
def test_assert_progress_carry_over_blocks_when_new_session_over_capacity(
    db, app, sample_user, sample_product
):
    """Carry-over is refused (BOTTLE_SESSION_CAPACITY_EXCEEDED) when the new
    session cannot cover the carried order, and the binding stays on A."""
    driver = _make_driver(db, "+998901000107", "OverCap")
    session_a = _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=5)
    delivery = _make_delivery(db, order, driver)

    svc = BottleTrackingService()
    svc.bind_order_to_session(session_a.id, order.id, accepted_by_driver_id=driver.id)

    session_a.status = DriverBottleSessionStatus.FORCE_CLOSED
    db.session.flush()
    # New session loaded with fewer bottles (3) than the order needs (5).
    session_b = _open_session(db, driver)
    session_b.bottles_loaded = 3
    db.session.flush()

    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            with pytest.raises(ValidationError) as exc:
                svc.assert_driver_can_progress_delivery(delivery)
            assert exc.value.error_code == "BOTTLE_SESSION_CAPACITY_EXCEEDED"
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False

    # Binding untouched — still on the old session.
    binding = DriverBottleSessionOrder.query.filter_by(order_id=order.id).first()
    assert binding.session_id == session_a.id


@pytest.mark.unit
def test_rebind_order_to_session_moves_binding_in_place(db, sample_user, sample_product):
    """rebind_order_to_session updates the existing row, is idempotent, and
    creates a binding when none exists."""
    driver = _make_driver(db, "+998901000108", "Rebind")
    driver2 = _make_driver(db, "+998901000118", "Rebind2")
    session_a = _open_session(db, driver)
    session_b = _open_session(db, driver2)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)

    svc = BottleTrackingService()
    # No binding yet → creates one.
    created = svc.rebind_order_to_session(order.id, session_a.id, accepted_by_driver_id=driver.id)
    assert created.session_id == session_a.id
    binding_id = created.id

    # Move it → same row, new session, new accepting driver.
    moved = svc.rebind_order_to_session(order.id, session_b.id, accepted_by_driver_id=driver2.id)
    assert moved.id == binding_id
    assert moved.session_id == session_b.id
    assert moved.accepted_by_driver_id == driver2.id

    # Idempotent when already on the target session.
    again = svc.rebind_order_to_session(order.id, session_b.id)
    assert again.id == binding_id
    assert again.session_id == session_b.id
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).count() == 1


@pytest.mark.unit
def test_new_session_close_releases_carried_binding(
    db, app, sample_user, sample_product
):
    """After a carried order migrates onto session B, closing B RELEASES that
    binding too — carried orders never lock a session (a driver closes anytime)."""
    driver = _make_driver(db, "+998901000109", "CarryClose")
    session_a = _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_delivery(db, order, driver)

    svc = BottleTrackingService()
    svc.bind_order_to_session(session_a.id, order.id, accepted_by_driver_id=driver.id)
    session_a.status = DriverBottleSessionStatus.FORCE_CLOSED
    db.session.flush()
    session_b = _open_session(db, driver)
    db.session.commit()

    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            svc.assert_driver_can_progress_delivery(delivery)  # migrates A→B
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False
    db.session.commit()
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).first().session_id == session_b.id

    # Order is still non-terminal (OUT_FOR_DELIVERY) → B's close releases it and closes.
    closed = svc.close_bottle_session(driver.id, bottles_returned_to_warehouse=session_b.bottles_loaded)
    assert closed.status == DriverBottleSessionStatus.CLOSED
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).first() is None


@pytest.mark.unit
def test_carry_over_end_to_end_tally_credits_new_session(
    db, app, sample_user, sample_product, monkeypatch
):
    """Full carry-over: delivering under the new session lands the bottle tally
    on the new session and leaves the old (closed) session untouched."""
    from shared.enums import DeliveryStatus
    from business_app.services.staff_service import StaffService

    driver = _make_driver(db, "+998901000110", "CarryE2E")
    session_a = _open_session(db, driver)
    session_a.bottles_loaded = 10
    db.session.flush()

    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_delivery(db, order, driver)
    delivery.status = DeliveryStatus.ARRIVED  # valid pre-condition for 'delivered'
    db.session.flush()

    svc = BottleTrackingService()
    svc.bind_order_to_session(session_a.id, order.id, accepted_by_driver_id=driver.id)

    # Force-close A, load a fresh session B.
    session_a.status = DriverBottleSessionStatus.FORCE_CLOSED
    db.session.flush()
    session_b = _open_session(db, driver)
    session_b.bottles_loaded = 10
    db.session.commit()

    # Keep the customer-ledger side effects out of scope.
    def _noop_delivered(self, order_id, user_id, address_id, quantity, actor_user_id):
        return None

    def _noop_returned(self, **_kwargs):
        return None

    monkeypatch.setattr(BottleTrackingService, "record_bottles_delivered", _noop_delivered)
    monkeypatch.setattr(BottleTrackingService, "record_bottles_returned", _noop_returned)

    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            StaffService.update_delivery_status(
                delivery.id,
                new_status="delivered",
                staff_user_id=driver.id,
                metadata={"bottles_returned": 1},
            )
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False

    db.session.refresh(session_a)
    db.session.refresh(session_b)
    binding = DriverBottleSessionOrder.query.filter_by(order_id=order.id).first()
    # Binding migrated, and the delivering session got the tally.
    assert binding.session_id == session_b.id
    assert session_b.bottles_delivered == 2
    assert session_b.bottles_collected_from_customers == 1
    # Old closed session untouched.
    assert (session_a.bottles_delivered or 0) == 0
    assert (session_a.bottles_collected_from_customers or 0) == 0


# ---------------------------------------------------------------------------
# close_bottle_session — releases bound undelivered orders so it can close anytime
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_close_session_releases_undelivered_bindings(
    db, sample_user, sample_product
):
    """A driver can close anytime: a bound, non-terminal order is RELEASED (its
    binding deleted) rather than blocking the close. The order itself stays
    non-terminal and re-binds to the driver's next session when next progressed."""
    from shared.enums import OrderStatus

    driver = _make_driver(db, "+998901000200", "CloseRelease")
    session = _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)

    svc = BottleTrackingService()
    svc.bind_order_to_session(session.id, order.id, accepted_by_driver_id=driver.id)
    db.session.commit()

    closed = svc.close_bottle_session(driver.id, bottles_returned_to_warehouse=session.bottles_loaded)

    assert closed.status == DriverBottleSessionStatus.CLOSED
    # The non-terminal binding was released, so nothing blocks the close.
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).first() is None
    assert svc._open_bindings_count_for_session(session.id) == 0
    # Releasing must NOT mark the order delivered — it stays for later completion.
    db.session.refresh(order)
    assert order.status == OrderStatus.OUT_FOR_DELIVERY


@pytest.mark.unit
def test_close_session_keeps_terminal_bindings_releases_open_ones(
    db, sample_user, sample_product
):
    """A mixed session keeps DELIVERED bindings (historical tally) and releases only
    the non-terminal ones; the delivered tally is untouched by the release."""
    from shared.enums import OrderStatus

    driver = _make_driver(db, "+998901000210", "MixedClose")
    session = _open_session(db, driver)
    delivered_order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    open_order = _make_order_with_bottles(db, sample_user, sample_product, quantity=3)

    svc = BottleTrackingService()
    svc.bind_order_to_session(session.id, delivered_order.id, accepted_by_driver_id=driver.id)
    svc.bind_order_to_session(session.id, open_order.id, accepted_by_driver_id=driver.id)
    delivered_order.status = OrderStatus.DELIVERED
    session.bottles_delivered = 2
    db.session.commit()

    closed = svc.close_bottle_session(driver.id, bottles_returned_to_warehouse=session.bottles_loaded)

    assert closed.status == DriverBottleSessionStatus.CLOSED
    # Delivered binding retained for the historical tally; open one released.
    assert DriverBottleSessionOrder.query.filter_by(order_id=delivered_order.id).first() is not None
    assert DriverBottleSessionOrder.query.filter_by(order_id=open_order.id).first() is None
    # The release does not disturb the sealed delivery tally.
    db.session.refresh(closed)
    assert closed.bottles_delivered == 2


@pytest.mark.unit
def test_close_then_new_session_rebinds_released_order(
    db, app, sample_user, sample_product
):
    """Full Rule-2 loop from a real close_bottle_session: release at close, then the
    order late-binds onto the driver's NEXT open session when they next progress it."""
    driver = _make_driver(db, "+998901000211", "RebindLoop")
    session_a = _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_delivery(db, order, driver)

    svc = BottleTrackingService()
    svc.bind_order_to_session(session_a.id, order.id, accepted_by_driver_id=driver.id)
    db.session.commit()

    svc.close_bottle_session(driver.id, bottles_returned_to_warehouse=session_a.bottles_loaded)
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).first() is None

    session_b = _open_session(db, driver)
    db.session.commit()

    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            result = svc.assert_driver_can_progress_delivery(delivery)
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False

    assert result is not None and result.id == session_b.id
    binding = DriverBottleSessionOrder.query.filter_by(order_id=order.id).first()
    assert binding is not None and binding.session_id == session_b.id


@pytest.mark.unit
def test_close_session_allowed_when_all_orders_delivered(
    db, sample_user, sample_product
):
    """Once bound orders reach a terminal status, close is allowed."""
    from shared.enums import OrderStatus

    driver = _make_driver(db, "+998901000201", "CloseOK")
    session = _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)

    svc = BottleTrackingService()
    svc.bind_order_to_session(session.id, order.id, accepted_by_driver_id=driver.id)
    order.status = OrderStatus.DELIVERED
    db.session.commit()

    closed = svc.close_bottle_session(driver.id, bottles_returned_to_warehouse=session.bottles_loaded)
    assert closed.status == DriverBottleSessionStatus.CLOSED


@pytest.mark.unit
def test_close_session_allowed_when_orders_cancelled(
    db, sample_user, sample_product
):
    """Cancelled orders don't block close — predicate uses Order.status, not binding existence."""
    from shared.enums import OrderStatus

    driver = _make_driver(db, "+998901000202", "CloseCancelled")
    session = _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)

    svc = BottleTrackingService()
    svc.bind_order_to_session(session.id, order.id, accepted_by_driver_id=driver.id)
    order.status = OrderStatus.CANCELLED
    db.session.commit()

    closed = svc.close_bottle_session(driver.id, bottles_returned_to_warehouse=session.bottles_loaded)
    assert closed.status == DriverBottleSessionStatus.CLOSED


@pytest.mark.unit
def test_admin_force_close_releases_open_bindings(db, sample_user, sample_product):
    """admin_force_close_session ignores the open-binding precondition AND mirrors the
    normal-close release, so an abandoned session's non-terminal orders are not left
    bound to a sealed session (which would corrupt its counters if later delivered)."""
    driver = _make_driver(db, "+998901000203", "ForceCloseDriver")
    admin = _make_driver(db, "+998901000204", "AdminUser")
    session = _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)

    svc = BottleTrackingService()
    svc.bind_order_to_session(session.id, order.id, accepted_by_driver_id=driver.id)
    db.session.commit()

    closed = svc.admin_force_close_session(
        session.id,
        actor_user_id=admin.id,
        bottles_returned_to_warehouse=0,
        reason="Driver went home with bottles",
    )
    assert closed.status == DriverBottleSessionStatus.FORCE_CLOSED
    # Non-terminal binding released on force-close too.
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).first() is None


# ---------------------------------------------------------------------------
# AD_000205_26 regression — accept under open session, close session, then
# attempting any further transition must fail (under strict enforcement).
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_assign_delivery_driver_blocks_cod_blocked_driver(db, app, sample_user, sample_product):
    """Auto/admin assign now enforces the COD-block gate via the SSOT."""
    from unittest.mock import patch
    from business_app.models.delivery import Delivery
    from business_app.services.delivery_service import DeliveryService
    from business_app.models.order import Order
    from shared.enums import PaymentMethod
    driver = _make_driver(db, "+998901700001", "CODBlocked")
    _make_driver_profile(db, driver, "+998901700001")
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=1)
    order.payment_method = PaymentMethod.CASH
    db.session.flush()
    delivery = _make_scheduled_unassigned_delivery(db, order)
    db.session.commit()
    with app.test_request_context(), \
         patch("business_app.services.driver_reconciliation_service.DriverReconciliationService.is_driver_blocked_from_cod", return_value=True), \
         patch.object(DeliveryService, "_notify_driver", lambda self, d: None), \
         patch.object(DeliveryService, "_optimize_driver_route", lambda self, *a, **k: None):
        with pytest.raises(ValidationError) as exc:
            DeliveryService().assign_delivery_driver(delivery.id, driver.id)
    assert exc.value.error_code == "STAFF_DRIVER_COD_BLOCKED"


@pytest.mark.unit
def test_regression_AD_000205_26_picked_up_blocked_after_session_close(
    db, app, sample_user, sample_product
):
    """
    Exact reproduction of the incident shape:
    1. Driver opens session S.
    2. Driver accepts order (binding S↔order created at accept).
    3. Session S is closed before the driver starts transit.
    4. Driver attempts to mark picked_up while having NO open session. Strict
       mode must raise BOTTLE_SESSION_REQUIRED — i.e. the driver is told to open
       a new session; the order is not lost. (Opening one then lets carry-over
       proceed — see test_carry_over_end_to_end_tally_credits_new_session.)
    """
    from business_app.services.staff_service import StaffService

    driver = _make_driver(db, "+998901000300", "RegressionDriver")
    session = _open_session(db, driver)
    session.bottles_loaded = 10
    db.session.flush()

    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=5)
    delivery = _make_delivery(db, order, driver)

    # Step 2: bind at accept (the new behaviour we just implemented).
    BottleTrackingService().bind_order_to_session(
        session.id, order.id, accepted_by_driver_id=driver.id
    )

    # Step 3: close the session under the driver's feet. Set the status directly
    # (rather than via close_bottle_session, which would release the binding) so the
    # order stays bound to a now-closed session — the state that forces the driver to
    # open a new session before they can progress it.
    session.status = DriverBottleSessionStatus.CLOSED
    db.session.flush()
    db.session.commit()

    # Step 4: with no open session to carry the order onto, picked_up must fail
    # under strict mode. Pre-fix, this transition went through silently and the
    # bottles tally was lost.
    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            with pytest.raises(ValidationError) as exc:
                StaffService.update_delivery_status(
                    delivery.id,
                    new_status="picked_up",
                    staff_user_id=driver.id,
                )
            assert exc.value.error_code == "BOTTLE_SESSION_REQUIRED"
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False


@pytest.mark.unit
def test_failed_delivery_releases_session_binding_so_session_can_close(
    db, app, sample_user, sample_product
):
    """Marking a delivery FAILED must release its bottle-session binding so the
    driver can still close the session.

    Reproduces the prod session-72 lockup (2026-06-26 & 2026-06-28): a failed
    delivery left its order bound and non-terminal (out_for_delivery), so
    close_bottle_session kept raising BOTTLE_SESSION_HAS_OPEN_ORDERS until an
    operator re-dispatched it. The order stays FAILED for operator review; only
    the bottle-session binding is released here.
    """
    from business_app.services.staff_service import StaffService
    from business_app.models.bottle import DriverBottleSessionOrder

    driver = _make_driver(db, "+998901000200", "FailGuy")
    session = _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_delivery(db, order, driver)  # ASSIGNED → 'failed' allowed
    BottleTrackingService().bind_order_to_session(
        session.id, order.id, accepted_by_driver_id=driver.id
    )
    db.session.commit()

    svc = BottleTrackingService()
    # Pre-condition: the bound, non-terminal order blocks the close.
    assert svc._open_bindings_count_for_session(session.id) == 1

    with app.test_request_context():
        StaffService.update_delivery_status(
            delivery.id,
            new_status="failed",
            staff_user_id=driver.id,
            metadata={"fail_reason": "customer_unavailable"},
        )

    # The failed attempt released the binding → nothing blocks the close.
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).first() is None
    assert svc._open_bindings_count_for_session(session.id) == 0

    db.session.refresh(session)
    closed = svc.close_bottle_session(
        driver.id, bottles_returned_to_warehouse=session.bottles_loaded
    )
    assert closed.status == DriverBottleSessionStatus.CLOSED
