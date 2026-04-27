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
from business_app.utils.constants import (
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
    from business_app.utils.constants import OrderStatus

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
    from business_app.utils.constants import OrderStatus

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
def test_staff_service_delivery_uses_session_tally_not_deprecated_load(
    db, sample_user, sample_product, monkeypatch
):
    """
    When a delivery is marked 'delivered', StaffService must:
      1. Call BottleTrackingService.update_session_delivery_tally (not the removed update_driver_delivery_counts)
      2. Call bind_order_to_session with accepted_by_driver_id = actual driver (not the actor)
    """
    from datetime import datetime, UTC, timedelta
    from business_app.models.delivery import Delivery
    from business_app.models.order import Order, OrderItem
    from business_app.models.user import UserAddress
    from business_app.services.staff_service import StaffService
    from business_app.utils.constants import OrderStatus

    # Enable returnable bottle tracking on the fixture product
    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    db.session.flush()

    # Create driver + open session
    driver = _make_driver(db, "+998901000008", "DeliveryGuy")
    session = _open_session(db, driver)
    session.bottles_loaded = 10
    db.session.flush()

    # Create customer address
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

    from business_app.utils.constants import DeliveryStatus
    delivery = Delivery(
        order_id=order.id,
        status=DeliveryStatus.ARRIVED,  # valid pre-condition for 'delivered' transition
        delivery_person_id=driver.id,
        scheduled_date=datetime.now(UTC) + timedelta(hours=1),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.flush()
    db.session.commit()

    # Capture calls to the two critical service methods
    tally_calls = []
    bind_calls = []

    original_tally = BottleTrackingService.update_session_delivery_tally
    original_bind = BottleTrackingService.bind_order_to_session

    def patched_tally(self, driver_id, *, bottles_delivered=0, bottles_collected=0):
        tally_calls.append({
            "driver_id": driver_id,
            "bottles_delivered": bottles_delivered,
            "bottles_collected": bottles_collected,
        })
        return original_tally(self, driver_id, bottles_delivered=bottles_delivered, bottles_collected=bottles_collected)

    def patched_bind(self, session_id, order_id, *, accepted_by_driver_id=None):
        bind_calls.append({
            "session_id": session_id,
            "order_id": order_id,
            "accepted_by_driver_id": accepted_by_driver_id,
        })
        return original_bind(self, session_id, order_id, accepted_by_driver_id=accepted_by_driver_id)

    def patched_record_delivered(self, order_id, user_id, address_id, quantity, actor_user_id):
        pass  # skip ledger entry side-effects to keep test focused

    monkeypatch.setattr(BottleTrackingService, "update_session_delivery_tally", patched_tally)
    monkeypatch.setattr(BottleTrackingService, "bind_order_to_session", patched_bind)
    monkeypatch.setattr(BottleTrackingService, "record_bottles_delivered", patched_record_delivered)

    StaffService.update_delivery_status(
        delivery.id,
        new_status="delivered",
        staff_user_id=driver.id,
        metadata={"bottles_returned": 1},
    )

    assert len(tally_calls) == 1, "update_session_delivery_tally must be called exactly once"
    assert tally_calls[0]["driver_id"] == driver.id
    assert tally_calls[0]["bottles_delivered"] == 2  # 2 units × 1 bottle each
    assert tally_calls[0]["bottles_collected"] == 1

    assert len(bind_calls) == 1, "bind_order_to_session must be called exactly once"
    assert bind_calls[0]["order_id"] == order.id
    assert bind_calls[0]["accepted_by_driver_id"] == driver.id  # actual driver, not admin


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
    from business_app.utils.constants import DriverSessionMembershipStatus as MStatus

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
