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
def test_assert_progress_strict_raises_when_no_binding(db, app, sample_user, sample_product):
    """Strict mode: missing binding → BOTTLE_SESSION_REQUIRED."""
    driver = _make_driver(db, "+998901000101", "Strict1")
    _open_session(db, driver)
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
def test_assert_progress_legacy_does_not_raise_when_no_binding(db, app, sample_user, sample_product):
    """Legacy mode: missing binding → return None, no raise (regression on bug-AD_000205_26)."""
    driver = _make_driver(db, "+998901000102", "Legacy1")
    _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_delivery(db, order, driver)

    svc = BottleTrackingService()
    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False
        # Must not raise — keeps in-flight orders unblocked during PR 1 measurement.
        result = svc.assert_driver_can_progress_delivery(delivery)
        assert result is None


@pytest.mark.unit
def test_assert_progress_strict_raises_when_bound_session_closed(
    db, app, sample_user, sample_product
):
    """Strict mode: bound session has been closed → BOTTLE_SESSION_CLOSED."""
    driver = _make_driver(db, "+998901000103", "Strict2")
    session = _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_delivery(db, order, driver)

    svc = BottleTrackingService()
    svc.bind_order_to_session(session.id, order.id, accepted_by_driver_id=driver.id)

    # Close the session out from under the bound order.
    session.status = DriverBottleSessionStatus.CLOSED
    db.session.flush()

    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            with pytest.raises(ValidationError) as exc:
                svc.assert_driver_can_progress_delivery(delivery)
            assert exc.value.error_code == "BOTTLE_SESSION_CLOSED"
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False


@pytest.mark.unit
def test_assert_progress_strict_raises_when_session_mismatch(
    db, app, sample_user, sample_product
):
    """Strict mode: driver's current effective session differs from bound session.

    Simulates a co-driver scenario where the order was bound under another
    driver's session while the delivery driver has their own separate session.
    """
    driver_a = _make_driver(db, "+998901000104", "DriverA")
    driver_b = _make_driver(db, "+998901000114", "DriverB")
    session_a = _open_session(db, driver_a)
    session_b = _open_session(db, driver_b)

    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_delivery(db, order, driver_a)

    svc = BottleTrackingService()
    # Order is bound to driver B's session even though delivery is assigned
    # to driver A — this is the mismatch we want to catch.
    svc.bind_order_to_session(session_b.id, order.id, accepted_by_driver_id=driver_b.id)
    db.session.flush()

    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            with pytest.raises(ValidationError) as exc:
                svc.assert_driver_can_progress_delivery(delivery)
            assert exc.value.error_code == "BOTTLE_SESSION_MISMATCH"
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False
    # session_a is referenced for symmetry / clarity above; mark used for linters.
    assert session_a is not None


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
# close_bottle_session — refuses to close when bound undelivered orders exist
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_close_session_blocked_when_undelivered_orders_bound(
    db, sample_user, sample_product
):
    """close_bottle_session raises BOTTLE_SESSION_HAS_OPEN_ORDERS when bindings exist."""
    driver = _make_driver(db, "+998901000200", "CloseBlocked")
    session = _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)

    svc = BottleTrackingService()
    svc.bind_order_to_session(session.id, order.id, accepted_by_driver_id=driver.id)
    db.session.commit()

    with pytest.raises(ValidationError) as exc:
        svc.close_bottle_session(driver.id, bottles_returned_to_warehouse=session.bottles_loaded)
    assert exc.value.error_code == "BOTTLE_SESSION_HAS_OPEN_ORDERS"


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
def test_admin_force_close_bypasses_open_bindings(db, sample_user, sample_product):
    """admin_force_close_session ignores the open-binding precondition."""
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


# ---------------------------------------------------------------------------
# AD_000205_26 regression — accept under open session, close session, then
# attempting any further transition must fail (under strict enforcement).
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_regression_AD_000205_26_picked_up_blocked_after_session_close(
    db, app, sample_user, sample_product
):
    """
    Exact reproduction of the incident shape:
    1. Driver opens session S.
    2. Driver accepts order (binding S↔order created at accept).
    3. Session S is closed before the driver starts transit.
    4. Driver attempts to mark picked_up. Strict mode must raise.
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

    # Step 3: close the session under the driver's feet. Skip the close-precondition
    # because we want to simulate the pre-fix world where the driver could have
    # walked off and admin had no warning — equivalent to admin_force_close from
    # the binding's perspective.
    session.status = DriverBottleSessionStatus.CLOSED
    db.session.flush()
    db.session.commit()

    # Step 4: attempting picked_up must now fail under strict mode. Pre-fix,
    # this transition went through silently and the bottles tally was lost.
    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            with pytest.raises(ValidationError) as exc:
                StaffService.update_delivery_status(
                    delivery.id,
                    new_status="picked_up",
                    staff_user_id=driver.id,
                )
            assert exc.value.error_code == "BOTTLE_SESSION_CLOSED"
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False
