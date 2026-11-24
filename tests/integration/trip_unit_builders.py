"""Setup-only builders for the bottle-trip tests (the 18.9 L bottle and the one-way 10 L unit).

Plain functions take `db`, so the Postgres races can call them with `pg_db`. Nothing here decides a rule: a test that
needs a row before the route that writes it exists inserts it with these, and the production code is what it asserts.
"""

import itertools
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.bottle import (
    DriverBottleSession,
    DriverBottleSessionOrder,
    DriverBottleSessionUnit,
    DriverSessionMembership,
)
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product, ProductCategory
from business_app.models.user import User, UserAddress
from business_app.tasks.delivery_monitoring_tasks import reoffer_pool_deliveries_task
from business_app.tasks.staff_tasks import push_bottle_event
from business_app.utils import slack_alerts
from shared.enums import (
    DeliveryStatus,
    DriverBottleSessionStatus,
    DriverSessionMembershipStatus,
    OrderStatus,
    PaymentMethod,
    UserRole,
    UserType,
)
from tests.integration.eta_plan_builders import make_driver as _make_rostered_driver
from tests.unit.test_sales_agent_tasks import _spy

ADMIN_SITE = "https://admin.aqua-element.uz"

_SEQ = itertools.count(1)


def trip_category(db) -> ProductCategory:
    category = ProductCategory(name=f"Water {next(_SEQ)}", description="Water products", is_active=True)
    db.session.add(category)
    db.session.commit()
    return category


def trip_customer(db) -> User:
    n = next(_SEQ)
    user = User(
        email=f"trip-customer-{n}@example.com",
        phone=f"+99893{n:07d}",
        password_hash="not-a-real-hash",
        first_name="Customer",
        last_name=f"No{n}",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def trip_driver(db, first_name: str, *, telegram_id: str) -> User:
    """A driver on shift all day, with the given first name (no last name) and Telegram id."""
    driver = _make_rostered_driver(db, roster=("00:00", "23:59"))
    driver.first_name = first_name
    driver.last_name = None
    driver.telegram_id = telegram_id
    db.session.query(DeliveryPerson).filter_by(user_id=driver.id).one().full_name = first_name
    db.session.commit()
    return driver


def nineteen(db, category) -> Product:
    """The returnable 18.9 L bottle: one bottle per unit."""
    product = Product(
        name="19 Litr suv",
        category_id=category.id,
        size="19L",
        base_price=Decimal("15000.00"),
        stock_quantity=1000,
        tracks_returnable_bottles=True,
        returnable_bottles_per_unit=1,
        is_active=True,
    )
    db.session.add(product)
    db.session.commit()
    return product


def ten_litre(db, category, *, counted: bool = True) -> Product:
    """The one-way 10 L water: no bottle comes back; `counted` is the product's trip setting."""
    product = Product(
        name="10 Litr suv",
        category_id=category.id,
        size="10L",
        base_price=Decimal("9000.00"),
        stock_quantity=1000,
        tracks_returnable_bottles=False,
        returnable_bottles_per_unit=0,
        counted_on_bottle_trip=counted,
        is_active=True,
    )
    db.session.add(product)
    db.session.commit()
    return product


def order_with_lines(
    db,
    customer,
    lines: List[Tuple[Product, int]],
    *,
    status: OrderStatus = OrderStatus.CONFIRMED,
    payment_method: PaymentMethod = PaymentMethod.CASH,
) -> Order:
    """An order of `(product, quantity)` lines, with an address."""
    n = next(_SEQ)
    address = UserAddress(
        user_id=customer.id,
        full_address=f"{n} Test St, Tashkent",
        street_address=f"{n} Test St",
        city="Tashkent",
        latitude=41.3111,
        longitude=69.2797,
        is_default=False,
    )
    db.session.add(address)
    db.session.flush()
    subtotal = sum((Decimal(p.base_price) * q for p, q in lines), Decimal("0.00"))
    order = Order(
        user_id=customer.id,
        order_number=f"ORD-TRIP-{n:06d}",
        status=status,
        payment_method=payment_method,
        delivery_address_id=address.id,
        subtotal=subtotal,
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=subtotal,
    )
    db.session.add(order)
    db.session.flush()
    for product, quantity in lines:
        db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=quantity,
                unit_price=product.base_price,
                total_price=Decimal(product.base_price) * quantity,
            )
        )
    db.session.commit()
    return order


def delivery_for(db, order, driver, status: DeliveryStatus = DeliveryStatus.ASSIGNED) -> Delivery:
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver.id if driver is not None else None,
        status=status,
        scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def trip(
    db,
    driver,
    *,
    bottles: int = 0,
    units: Optional[Dict[int, int]] = None,
    collected: int = 0,
    status: DriverBottleSessionStatus = DriverBottleSessionStatus.OPEN,
) -> DriverBottleSession:
    """A trip with `bottles` 18.9 L loaded and one `loaded` line per `units` entry ({product_id: count})."""
    n = next(_SEQ)
    session = DriverBottleSession(
        session_ref=f"TRIP-{n:06d}",
        driver_user_id=driver.id,
        status=status,
        bottles_loaded=bottles,
        bottles_collected_from_customers=collected,
        loaded_by_user_id=driver.id,
    )
    if status in (DriverBottleSessionStatus.CLOSED, DriverBottleSessionStatus.FORCE_CLOSED):
        session.closed_at = datetime.now(timezone.utc)
        session.closed_by_user_id = driver.id
        session.force_closed = status == DriverBottleSessionStatus.FORCE_CLOSED
    db.session.add(session)
    db.session.flush()
    for product_id, loaded in (units or {}).items():
        db.session.add(DriverBottleSessionUnit(session_id=session.id, product_id=product_id, loaded=loaded))
    db.session.commit()
    return session


def bind(db, session, order, driver) -> DriverBottleSessionOrder:
    link = DriverBottleSessionOrder(session_id=session.id, order_id=order.id, accepted_by_driver_id=driver.id)
    db.session.add(link)
    db.session.commit()
    return link


def member(db, session, driver) -> DriverSessionMembership:
    """An ACTIVE co-driver membership on `session`."""
    membership = DriverSessionMembership(
        session_id=session.id,
        session_owner_id=session.driver_user_id,
        member_driver_id=driver.id,
        status=DriverSessionMembershipStatus.ACTIVE,
    )
    db.session.add(membership)
    db.session.commit()
    return membership


def staff_headers(driver) -> dict:
    """A staff token (staff routes read the DB roles, not a claim). Call inside an app context."""
    return {
        "Authorization": f"Bearer {create_access_token(identity=str(driver.id))}",
        "Content-Type": "application/json",
    }


def strict(app, monkeypatch, on: bool) -> None:
    monkeypatch.setitem(app.config, "BOTTLE_SESSION_ENFORCEMENT_STRICT", on)


def pushes(enqueued) -> List[tuple]:
    """Each `push_bottle_event` publish's args, unchanged: `(telegram_id, event, payload)`."""
    return [args for name, args, _kwargs in enqueued if name == push_bottle_event.name]


def reoffers(enqueued) -> int:
    """How many `reoffer_pool_deliveries_task` publishes there were."""
    return sum(1 for name, _args, _kwargs in enqueued if name == reoffer_pool_deliveries_task.name)


@pytest.fixture
def slack(app, monkeypatch):
    """Every post to the admins' Slack alerts channel, as `[((text,), kwargs)]`; Slack accepts each."""
    monkeypatch.setitem(app.config, "ADMIN_UI_BASE_URL", ADMIN_SITE)
    return _spy(monkeypatch, slack_alerts, "send_slack_alert", result=True)


@pytest.fixture
def big(db, sample_category):
    return nineteen(db, sample_category)


@pytest.fixture
def small(db, sample_category):
    return ten_litre(db, sample_category)


@pytest.fixture
def driver(db):
    return trip_driver(db, "Dilshod", telegram_id="900000001")


@pytest.fixture
def owner(db):
    return trip_driver(db, "Olim", telegram_id="900000002")


@pytest.fixture
def co_driver(db):
    return trip_driver(db, "Kamol", telegram_id="900000003")


@pytest.fixture
def sender(db):
    return trip_driver(db, "Aziz", telegram_id="900000004")


@pytest.fixture
def receiver(db):
    return trip_driver(db, "Bobur", telegram_id="900000005")
