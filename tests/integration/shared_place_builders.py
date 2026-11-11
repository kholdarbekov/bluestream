"""Builders for the shared-place detection suites (plan 2026-11-11).

THE builders module of the plan: later tasks extend it, none starts a second one. Phones come from the unused
block `+99879xxxxxxx`; pins sit inside Tashkent near BASE with explicit coordinates (the default fixtures all
share one point, which would put every neighbour at distance zero). 0.0001 degrees of latitude is ~11.1 m.
"""

from datetime import UTC, datetime
from decimal import Decimal
from itertools import count

from business_app import db as _db
from business_app.models.customer_link import AddressGroup
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order, OrderItem
from business_app.models.payment import Payment
from business_app.models.product import Product, ProductCategory
from business_app.models.user import UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import (
    DeliveryStatus,
    EntitySubtype,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserType,
)
from tests.integration.reorder_reminder_builders import make_customer

BASE_LAT, BASE_LNG = 41.31, 69.28
_PHONES = count(1)
_STOPS = count(1)
UNIT_PRICE = Decimal("15000.00")


def customer(db, first_name, *, user_type=UserType.INDIVIDUAL, grocery=False):
    """A customer account. `grocery=True` builds the grocery-store entity (grocery stores are ENTITY accounts)."""
    if grocery:
        user_type = UserType.ENTITY
    return make_customer(
        db,
        first_name=first_name,
        phone=f"+99879{next(_PHONES):07d}",
        user_type=user_type,
        entity_subtype=EntitySubtype.GROCERY_STORE if grocery else None,
        company_name=f"{first_name} Shop" if user_type == UserType.ENTITY else None,
    )


def pinned_address(db, user, lat, lng, *, apartment=None):
    """An ungrouped address of `user` pinned at (lat, lng)."""
    address = UserAddress(
        user_id=user.id,
        title="home",
        full_address=f"{user.id} Test St, Tashkent",
        street_address=f"{user.id} Test St",
        city="Tashkent",
        latitude=lat,
        longitude=lng,
        apartment_number=apartment,
    )
    db.session.add(address)
    db.session.commit()
    return address


def empties_at(address, n):
    """`n` empties on record at the address's place, through `set_initial_balance` (no order)."""
    BottleTrackingService().set_initial_balance(None, address.id, Decimal(n), actor_user_id=address.user_id)


def group_of(db, *addresses, label="office"):
    """Put the addresses into one new place group (call before `empties_at`, so the balance is the group's)."""
    group = AddressGroup(label=label)
    db.session.add(group)
    db.session.flush()
    for address in addresses:
        address.address_group_id = group.id
    db.session.commit()
    return group


def driver(db, first_name):
    """A delivery driver with an active `DeliveryPerson` profile (the staff routes refuse a deactivated one)."""
    user = make_customer(
        db,
        first_name=first_name,
        phone=f"+99879{next(_PHONES):07d}",
        role=UserRole.DELIVERY_DRIVER,
        user_type=UserType.STAFF,
    )
    db.session.add(
        DeliveryPerson(
            user_id=user.id, full_name=f"{first_name} Driver", phone=user.phone, is_active=True, is_available=True
        )
    )
    db.session.commit()
    return user


def stop_for(customer, address, driver, *, quantity):
    """`customer`'s order of `quantity` returnable bottles to `address`, OUT_FOR_DELIVERY, with `driver`'s delivery
    ARRIVED at the door. Returns `(order, delivery)`.

    The shape of test_staff_bot_place_full_e2e.py `_delivery_for`: CARD-paid with a COMPLETED payment, so the
    cash-collection engine stays out and the bottles are what the delivery moves. `driver=None` builds the order alone,
    with no Delivery row: the order the admin Orders "Bottles Returned" path delivers.
    """
    n = next(_STOPS)
    category = ProductCategory(name=f"Shared Place Water {n}", description="w", is_active=True)
    _db.session.add(category)
    _db.session.flush()
    product = Product(
        name=f"Shared Place Water 19L {n}",
        description="d",
        category_id=category.id,
        size="19L",
        volume=19.0,
        volume_unit="L",
        base_price=UNIT_PRICE,
        stock_quantity=100,
        min_stock_level=1,
        max_stock_level=500,
        is_active=True,
        tracks_returnable_bottles=True,
        returnable_bottles_per_unit=Decimal("1"),
    )
    _db.session.add(product)
    _db.session.flush()
    total = UNIT_PRICE * quantity
    order = Order(
        user_id=customer.id,
        order_number=f"ORD-SP-{n:05d}",
        status=OrderStatus.OUT_FOR_DELIVERY,
        subtotal=total,
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=total,
        payment_method=PaymentMethod.CARD,
        delivery_address_id=address.id,
    )
    _db.session.add(order)
    _db.session.flush()
    _db.session.add(
        OrderItem(order_id=order.id, product_id=product.id, quantity=quantity, unit_price=UNIT_PRICE, total_price=total)
    )
    _db.session.add(
        Payment(
            order_id=order.id,
            user_id=customer.id,
            payment_method=PaymentMethod.CARD,
            amount=total,
            amount_collected=total,
            outstanding_amount=Decimal("0.00"),
            currency="UZS",
            status=PaymentStatus.COMPLETED,
            payment_id=f"card_sp_{n}",
        )
    )
    delivery = None
    if driver is not None:
        delivery = Delivery(
            order_id=order.id,
            delivery_person_id=driver.id,
            status=DeliveryStatus.ARRIVED,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="09:00-12:00",
        )
        _db.session.add(delivery)
    _db.session.commit()
    return order, delivery
