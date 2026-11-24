"""Free full bottles: the one capacity number (spec A8).

free = full bottles on board - returnable bottles of the stops the crew has set off on
(IN_TRANSIT, ARRIVED). Empties never count; ASSIGNED / PICKED_UP stops do not reserve.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count

import pytest

from business_app.models.bottle import DriverBottleSession, DriverSessionMembership
from business_app.models.delivery import Delivery
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product, ProductSizeEnum
from business_app.models.user import User, UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService, TripUnits
from business_app.utils.exceptions import ValidationError
from business_app.utils.state_validators import DELIVERY_BOTTLES_PROMISED_STATES
from shared.enums import (
    DeliveryStatus,
    DriverBottleSessionStatus,
    DriverSessionMembershipStatus,
    OrderStatus,
    UserRole,
    UserType,
)

_seq = count(1)


@pytest.fixture
def returnable_19l(db, sample_category):
    product = Product(
        name="19 litrlik suv",
        category_id=sample_category.id,
        size=ProductSizeEnum.SIZE_19L,
        volume=19.0,
        volume_unit="L",
        base_price=Decimal("15000.00"),
        stock_quantity=500,
        is_active=True,
        tracks_returnable_bottles=True,
        returnable_bottles_per_unit=Decimal("1.00"),
    )
    db.session.add(product)
    db.session.commit()
    return product


@pytest.fixture
def customer(db):
    user = User(
        phone="+998900000777",
        password_hash="x",
        first_name="C",
        last_name="",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def driver(delivery_driver):
    return delivery_driver


@pytest.fixture
def svc():
    return BottleTrackingService()


def _session(db, driver, *, loaded, collected=0, transferred_out=0, reopened_at=None):
    session = DriverBottleSession(
        driver_user_id=driver.id,
        bottles_loaded=loaded,
        bottles_collected_from_customers=collected,
        bottles_transferred_out=transferred_out,
        status=DriverBottleSessionStatus.OPEN,
        reopened_at=reopened_at,
    )
    db.session.add(session)
    db.session.commit()
    return session


def _delivery(db, customer, product, driver_id, status, bottles):
    n = next(_seq)
    addr = UserAddress(user_id=customer.id, title="Stop", full_address=f"Stop {n}", street_address=f"Stop {n}")
    db.session.add(addr)
    db.session.flush()
    order = Order(
        user_id=customer.id,
        order_number=f"FF_{n:05d}",
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("15000") * bottles,
        total_amount=Decimal("15000") * bottles,
        delivery_address_id=addr.id,
        delivery_date=datetime.now(UTC) + timedelta(hours=2),
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=bottles,
            unit_price=Decimal("15000.00"),
            total_price=Decimal("15000.00") * bottles,
        )
    )
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver_id,
        status=status,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


@pytest.mark.unit
def test_promised_states_are_exactly_on_the_way_and_arrived():
    assert DELIVERY_BOTTLES_PROMISED_STATES == frozenset({DeliveryStatus.IN_TRANSIT, DeliveryStatus.ARRIVED})


@pytest.mark.unit
def test_free_full_subtracts_only_on_the_way_and_arrived(db, svc, driver, customer, returnable_19l):
    _session(db, driver, loaded=10)
    _delivery(db, customer, returnable_19l, driver.id, DeliveryStatus.ASSIGNED, 3)
    _delivery(db, customer, returnable_19l, driver.id, DeliveryStatus.PICKED_UP, 2)
    _delivery(db, customer, returnable_19l, driver.id, DeliveryStatus.IN_TRANSIT, 4)
    _delivery(db, customer, returnable_19l, driver.id, DeliveryStatus.ARRIVED, 1)
    assert svc.free_full_bottles(driver.id) == 5


@pytest.mark.unit
def test_free_full_ignores_empties(db, svc, driver):
    _session(db, driver, loaded=3, collected=15)
    assert svc.free_full_bottles(driver.id) == 3


@pytest.mark.unit
def test_free_full_crew_shares_one_number(db, svc, driver, second_delivery_driver, customer, returnable_19l):
    session = _session(db, driver, loaded=10)
    db.session.add(
        DriverSessionMembership(
            session_id=session.id,
            session_owner_id=driver.id,
            member_driver_id=second_delivery_driver.id,
            status=DriverSessionMembershipStatus.ACTIVE,
        )
    )
    db.session.commit()
    _delivery(db, customer, returnable_19l, second_delivery_driver.id, DeliveryStatus.IN_TRANSIT, 4)

    assert svc.crew_driver_ids(session) == sorted([driver.id, second_delivery_driver.id])
    assert svc.free_full_bottles(driver.id) == 6
    assert svc.free_full_bottles(second_delivery_driver.id) == 6


@pytest.mark.unit
def test_revoked_member_is_not_crew(db, svc, driver, second_delivery_driver, customer, returnable_19l):
    session = _session(db, driver, loaded=10)
    db.session.add(
        DriverSessionMembership(
            session_id=session.id,
            session_owner_id=driver.id,
            member_driver_id=second_delivery_driver.id,
            status=DriverSessionMembershipStatus.LEFT,
        )
    )
    db.session.commit()
    _delivery(db, customer, returnable_19l, second_delivery_driver.id, DeliveryStatus.IN_TRANSIT, 4)
    assert svc.crew_driver_ids(session) == [driver.id]
    assert svc.free_full_bottles_for_session(session) == 10


@pytest.mark.unit
def test_free_full_excludes_given_delivery(db, svc, driver, customer, returnable_19l):
    session = _session(db, driver, loaded=8)
    d1 = _delivery(db, customer, returnable_19l, driver.id, DeliveryStatus.IN_TRANSIT, 8)
    assert svc.free_full_bottles(driver.id) == 0
    assert svc.free_full_bottles(driver.id, exclude_delivery_id=d1.id) == 8
    assert svc.free_full_bottles_for_session(session, exclude_delivery_id=d1.id) == 8


@pytest.mark.unit
def test_free_full_clamps_legacy_negative(db, svc, driver):
    _session(db, driver, loaded=0, transferred_out=4)
    assert svc.free_full_bottles(driver.id) == 0


@pytest.mark.unit
def test_free_full_clamps_when_promised_exceeds_loaded(db, svc, driver, customer, returnable_19l):
    _session(db, driver, loaded=2)
    _delivery(db, customer, returnable_19l, driver.id, DeliveryStatus.IN_TRANSIT, 5)
    assert svc.free_full_bottles(driver.id) == 0


@pytest.mark.unit
def test_reopened_session_has_zero_free_full(db, svc, driver):
    session = _session(db, driver, loaded=10, reopened_at=datetime.now(UTC))
    assert svc.free_full_bottles_for_session(session) == 0
    assert svc.free_full_bottles(driver.id) == 0


@pytest.mark.unit
def test_no_session_returns_none(db, svc, driver):
    assert svc.free_full_bottles(driver.id) is None


@pytest.mark.unit
def test_capacity_error_details_use_free_full(db, svc, driver):
    session = _session(db, driver, loaded=3, collected=15)
    with pytest.raises(ValidationError) as excinfo:
        svc.assert_order_within_session_capacity(session, TripUnits(Decimal(8), {}))
    assert excinfo.value.error_code == "BOTTLE_SESSION_CAPACITY_EXCEEDED"
    assert excinfo.value.details == {"available": 3, "required": 8, "shortfall": 5}


@pytest.mark.unit
def test_capacity_passes_when_free_full_covers_it(db, svc, driver, customer, returnable_19l):
    session = _session(db, driver, loaded=10)
    d = _delivery(db, customer, returnable_19l, driver.id, DeliveryStatus.IN_TRANSIT, 8)
    svc.assert_order_within_session_capacity(session, TripUnits(Decimal(8), {}), exclude_delivery_id=d.id)
    with pytest.raises(ValidationError):
        svc.assert_order_within_session_capacity(session, TripUnits(Decimal(8), {}))
