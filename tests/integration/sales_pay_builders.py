"""Builders for the sales-agent pay and compliance suites (spec §10.1; plan contract C30).

A builder writes the row the REAL writer writes, and nothing it would not. `make_visit` is
pinned against the real staff visit loop by T-VIS-7
(tests/integration/test_sales_compliance_one_producer.py). Instants are always explicit:
`VisitService.start` stamps the wall clock, so a loop-driven visit cannot land on a chosen local
day, and the container runs in UTC while the app reasons in Asia/Tashkent.
"""

import inspect
import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import count
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.product import Product
from business_app.models.sales import Outlet
from business_app.models.sales_pay import SalesAgentUnpaidDay, SalesPayPeriod
from business_app.models.sales_visits import SalesAgentDayPlan, Visit
from business_app.models.user import User
from business_app.services.sales import visit_service as visit_service_module
from business_app.services.sales.visit_rules import short_visit_threshold
from business_app.utils import delivery_window, local_windows
from business_app.utils.local_windows import local_day_bounds
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import UserRole
from tests.unit.test_outlet_dedupe import PIN

_OUTLET_NUMBERS = count(1)

# What the real loop leaves on a visit that was started, checked in, stock-checked and closed
# with no order (`VisitService.start` -> `checkin` -> `stock_check` -> `close`): `stock_check`
# moves the step to "order" and `close` does not move it again. T-VIS-7 holds this dict to the
# real rows; a test overrides a key only to describe a DIFFERENT visit (abandoned, ordered...).
_REAL_NO_ORDER_VISIT = {
    "status": "completed",
    "current_step": "order",
    "planned": False,
    "checkin_skipped": False,
    "outcome": "no_order",
    "no_order_reason": "sufficient_stock",
}
# NOT NULL with no default: a visit without them is not a visit.
_VISIT_REQUIRED = ("outlet_id", "agent_user_id", "started_at")


def freeze_local_now(monkeypatch, when: datetime, *, visit_service: bool = False) -> datetime:
    """Freeze the business clock at `when` (an aware instant) and return it in Tashkent time.

    Patches the name in `local_windows` (every window, `local_date()` default and "today") and in
    `delivery_window` (delivery-date validation); `visit_service=True` also patches the copy
    `visit_service` bound by name, which `place_order` reads for the local day. The frozen value
    is a LOCAL-zone datetime like the real `local_now()`'s, because `local_windows._tz()` borrows
    its tzinfo: a UTC-zoned freeze would quietly make every "local day" a UTC day.
    """
    if when.tzinfo is None:
        raise ValueError("freeze_local_now needs an aware instant")
    frozen = when.astimezone(ZoneInfo(DISPLAY_TIMEZONE))
    monkeypatch.setattr(local_windows, "local_now", lambda: frozen)
    monkeypatch.setattr(delivery_window, "local_now", lambda: frozen)
    if visit_service:
        monkeypatch.setattr(visit_service_module, "local_now", lambda: frozen)
    return frozen


def make_outlet(
    db,
    *,
    onboarded_by,
    created_at: datetime,
    user=None,
    pin: Optional[Tuple[float, float]] = None,
    stage: str = "active",
    assigned_agent=None,
    outlet_class: str = "B",
) -> Outlet:
    """One outlet row carrying exactly the ownership facts a test names.

    `onboarded_by` is the agent credited with registering it (written once, at creation);
    `assigned_agent` is its current owner, and Q10 makes the due scope follow the owner. `user`
    links the customer account; `address_id` stays NULL, so `OutletService.order_scope` reads the
    whole account, the single-outlet rule. `pin` is `(lat, lng)`: without one, a check-in there
    cannot be measured (`in_radius` NULL, the `no_location` reason).
    """
    outlet = Outlet(
        name=f"Builder outlet {next(_OUTLET_NUMBERS):03d}",
        outlet_type="grocery_store",
        stage=stage,
        outlet_class=outlet_class,
        district="chilanzar",
        user_id=user.id if user is not None else None,
        latitude=pin[0] if pin is not None else None,
        longitude=pin[1] if pin is not None else None,
        assigned_agent_user_id=assigned_agent.id if assigned_agent is not None else None,
        onboarded_by_user_id=onboarded_by.id if onboarded_by is not None else None,
        created_at=created_at,
    )
    db.session.add(outlet)
    db.session.commit()
    return outlet


def make_visit(db, **explicit_fields) -> Visit:
    """One `Visit` row: the real no-order loop's columns, overridden by `explicit_fields`.

    Every field is a `Visit` column name; an unknown one is a TypeError from the model, never a
    silently ignored keyword. `outlet_id`, `agent_user_id` and `started_at` are required. The
    check-in facts (`checkin_at`, `in_radius`, `distance_m`, `checkin_skipped`) and `ended_at`
    are whatever the test says: those are the inputs the verified-visit rule reads.
    """
    missing = [name for name in _VISIT_REQUIRED if name not in explicit_fields]
    if missing:
        raise TypeError(f"make_visit needs {', '.join(missing)}")
    visit = Visit(**{**_REAL_NO_ORDER_VISIT, **explicit_fields})
    db.session.add(visit)
    db.session.commit()
    return visit


def make_day_plan(
    db,
    agent,
    plan_date: date,
    due_outlet_ids: Optional[List[int]],
    *,
    due_count: Optional[int] = None,
) -> SalesAgentDayPlan:
    """One `sales_agent_day_plans` row, as the 01:20 job writes it for `plan_date`.

    With ids, `due_count` is their number, the pair the snapshot always writes together (spec
    §4.1.2), and a disagreeing `due_count` is refused. `due_outlet_ids=None` is a LEGACY row,
    written before the set was frozen, and then `due_count` is required.
    """
    if due_outlet_ids is None:
        if due_count is None:
            raise ValueError("a legacy day plan (no due_outlet_ids) needs its due_count")
    elif due_count is not None and due_count != len(due_outlet_ids):
        raise ValueError("the snapshot writes due_count = len(due_outlet_ids)")
    row = SalesAgentDayPlan(
        agent_user_id=agent.id,
        plan_date=plan_date,
        due_count=len(due_outlet_ids) if due_outlet_ids is not None else due_count,
        overdue_count=0,
        due_outlet_ids=sorted(due_outlet_ids) if due_outlet_ids is not None else None,
        # 01:20 local on the plan's own day: the job's hour.
        snapshot_at=local_day_bounds(plan_date)[0] + timedelta(hours=1, minutes=20),
    )
    db.session.add(row)
    db.session.commit()
    return row


# --------------------------------------------------------------------------- #
# Task 2: the field week (T-VIS-1, T-VIS-3, T-VIS-6).
#
# One October week that exercises every not-counted reason and every excluded day. Its
# day-by-day table is the module docstring of tests/integration/test_sales_compliance_one_producer.py;
# Task 10's pay gate (T-VIS-1) reads the same week. Import the fixture with
# `from tests.integration.sales_pay_builders import field_week  # noqa: F401 -- a fixture`.
# --------------------------------------------------------------------------- #

# Measured days are 10-01, 10-02, 10-03, 10-04 and 10-07: due = 3 + 4 + 2 + 1 + 2 = 12, counted =
# 1 + 1 + 0 + 1 + 2 = 5, and 5 / 12 = 41.66...% publishes 41.7 (`AgentMetricsService.pct`, one
# decimal). Sunday 10-04 is a worked day like any other (C2 v5, I-39).
FIELD_WEEK_DUE, FIELD_WEEK_COUNTED, FIELD_WEEK_PCT = 12, 5, 41.7

# Thursday 10-08 18:00 Tashkent, so `period=month` is 10-01..10-08.
_FIELD_WEEK_FROZEN_LOCAL = datetime(2026, 10, 8, 18, 0, tzinfo=ZoneInfo(DISPLAY_TIMEZONE))
# Every field-week shop was onboarded long before the week.
_FIELD_WEEK_LONG_AGO = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)


def _field_week_local(day, hour, minute=0, second=0):
    """An October local instant, stored in UTC as the visit loop stores it."""
    return datetime(2026, 10, day, hour, minute, second, tzinfo=ZoneInfo(DISPLAY_TIMEZONE)).astimezone(timezone.utc)


def _field_week_visit(db, agent, outlet, day, *, seconds=600, hour=10, **fields):
    """A visit on a local October day: started at `hour`, checked in two minutes later, closed
    `seconds` after the check-in, measured in range unless `fields` say otherwise."""
    started_at = _field_week_local(day, hour)
    checkin_at = started_at + timedelta(minutes=2)
    values = {
        "outlet_id": outlet.id,
        "agent_user_id": agent.id,
        "started_at": started_at,
        "checkin_at": checkin_at,
        "ended_at": checkin_at + timedelta(seconds=seconds),
        "in_radius": True,
        "distance_m": 14.0,
        **fields,
    }
    return make_visit(db, **values)


@pytest.fixture
def field_week(db, monkeypatch, sales_agent_user, admin_user):
    """The field week, for the conftest agent (profile with no employment dates: employed on every
    day). Returns the agent, the short-visit threshold and the two threshold-edge visits."""
    freeze_local_now(monkeypatch, _FIELD_WEEK_FROZEN_LOCAL)
    agent = sales_agent_user
    threshold = short_visit_threshold()

    def _shop(**kwargs):
        return make_outlet(db, onboarded_by=agent, created_at=_FIELD_WEEK_LONG_AGO, assigned_agent=agent, **kwargs)

    one, two, three, four, six = (_shop(pin=PIN) for _ in range(5))
    pinless = _shop()

    # Thu 10-01, worked.
    make_day_plan(db, agent, date(2026, 10, 1), [one.id, two.id, three.id])
    at_threshold = _field_week_visit(db, agent, one, 1, seconds=threshold)
    one_second_short = _field_week_visit(db, agent, two, 1, seconds=threshold - 1)
    _field_week_visit(db, agent, three, 1, checkin_skipped=True, in_radius=None, distance_m=None)
    # Fri 10-02, worked.
    make_day_plan(db, agent, date(2026, 10, 2), [one.id, four.id, pinless.id, six.id])
    _field_week_visit(db, agent, one, 2, in_radius=False, distance_m=850.0)
    _field_week_visit(db, agent, four, 2, hour=10)
    _field_week_visit(db, agent, four, 2, hour=15)
    _field_week_visit(db, agent, pinless, 2, in_radius=None, distance_m=None)
    _field_week_visit(
        db,
        agent,
        six,
        2,
        checkin_at=None,
        ended_at=_field_week_local(2, 10, 20),
        in_radius=None,
        distance_m=None,
        current_step="checkin",
    )
    # Sat 10-03, worked.
    make_day_plan(db, agent, date(2026, 10, 3), [one.id, two.id])
    _field_week_visit(db, agent, one, 3, status="abandoned", current_step="stock", outcome=None, no_order_reason=None)
    _field_week_visit(db, agent, three, 3, planned=True)
    # Sun 10-04, worked (every day is a working day, C2 v5): one due, verified, counted.
    make_day_plan(db, agent, date(2026, 10, 4), [one.id])
    _field_week_visit(db, agent, one, 4)
    # Mon 10-05: an unpaid day.
    db.session.add(
        SalesAgentUnpaidDay(
            agent_user_id=agent.id,
            unpaid_date=date(2026, 10, 5),
            note="Family leave",
            created_by_user_id=admin_user.id,
        )
    )
    db.session.commit()
    make_day_plan(db, agent, date(2026, 10, 5), [one.id, two.id])
    _field_week_visit(db, agent, one, 5)
    # Tue 10-06: worked, but the 01:20 job left no row.
    _field_week_visit(db, agent, one, 6)
    # Wed 10-07: a legacy row, written before the due set was frozen.
    make_day_plan(db, agent, date(2026, 10, 7), None, due_count=2)
    for shop in (one, two, three):
        _field_week_visit(db, agent, shop, 7, planned=True)
    _field_week_visit(db, agent, four, 7)
    # Thu 10-08: a company holiday.
    db.session.add(
        SalesPayPeriod(
            month_start=date(2026, 10, 1),
            status="open",
            is_shadow=False,
            holidays=[{"date": "2026-10-08", "note": "Company holiday"}],
        )
    )
    db.session.commit()
    make_day_plan(db, agent, date(2026, 10, 8), [one.id])
    _field_week_visit(db, agent, one, 8)

    return SimpleNamespace(
        agent=agent, threshold=threshold, at_threshold=at_threshold, one_second_short=one_second_short
    )


# --------------------------------------------------------------------------- #
# Task 4: the pay audit reader.
# --------------------------------------------------------------------------- #


def pay_audit(events, verb):
    """The `sales_pay_<verb>` audit calls (SETTINGS_CHANGED, HIGH), as (resource_type, resource_id, old, new).

    `events` is the `audit_events` recorder (tests/integration/test_admin_order_reschedule.py),
    bound to the real `AuditLogger.log_event` signature.
    """
    from business_app.utils.audit_logger import AuditEventType, AuditSeverity

    return [
        (event["resource_type"], event["resource_id"], event["old_values"], event["new_values"])
        for event in events
        if event["action"] == f"sales_pay_{verb}"
        and event["event_type"] == AuditEventType.SETTINGS_CHANGED
        and event["severity"] == AuditSeverity.HIGH
    ]


# --------------------------------------------------------------------------- #
# Task 5: shops, products, plan payloads and the orders the ledger sync reads.
#
# Every order goes through the real writers (spec §10.1): OrderService.create_order,
# update_order_status (CONFIRMED while the order is still PENDING, then DELIVERED) and
# CashCollectionService.post_collection for the money, whose `occurred_at` becomes the
# paid instant. The DELIVERED history row is the one timestamp a builder rewrites
# (`backdate_delivered`); created_at / updated_at stay the wall clock.
# --------------------------------------------------------------------------- #

_OFFICE_PHONE = "+998939999999"
_DRIVER_PHONE = "+998939999998"
_STORE_SEQUENCE = {"last": 0}


def _office(db):
    """The admin every builder write is recorded as (confirm, deliver, collect).

    A completed cash payment must name who collected it
    (`ck_payments_cash_completed_requires_collector`); a standalone collection with no
    driver stamps its recorder, so the builders always record one.
    """
    from business_app.models.user import User
    from shared.enums import UserRole, UserType

    user = User.query.filter_by(phone=_OFFICE_PHONE).first()
    if user is None:
        user = User(
            phone=_OFFICE_PHONE,
            email="office-cashier@example.com",
            password_hash="x" * 60,
            first_name="Office",
            last_name="Cashier",
            user_type=UserType.STAFF,
            role=UserRole.ADMIN,
            is_verified=True,
        )
        db.session.add(user)
        db.session.commit()
    return user


def _driver(db):
    """The driver every builder delivery is handed to: a staff user with an active
    `DeliveryPerson` profile, the identity `DeliveryAssignmentService.assign_driver` resolves."""
    from business_app.models.delivery import DeliveryPerson
    from business_app.models.user import User
    from shared.enums import UserRole, UserType

    user = User.query.filter_by(phone=_DRIVER_PHONE).first()
    if user is None:
        user = User(
            phone=_DRIVER_PHONE,
            email="builder-driver@example.com",
            password_hash="x" * 60,
            first_name="Builder",
            last_name="Driver",
            user_type=UserType.STAFF,
            role=UserRole.DELIVERY_DRIVER,
            is_verified=True,
        )
        db.session.add(user)
        db.session.flush()
        db.session.add(DeliveryPerson(user_id=user.id, full_name="Builder Driver", phone=_DRIVER_PHONE, is_active=True))
        db.session.commit()
    return user


def _drive_to_the_door(db, order_id: int, office) -> None:
    """Hand a released order's delivery to the builder driver and bring it to the door, so the
    office's DELIVERED can complete it.

    Confirming a due order puts its delivery in the pool (`ensure_delivery_if_due`), and the
    DELIVERED writer completes that delivery, which needs a driver on file
    (`assert_delivery_person_for_status`). An assigned delivery moves PICKED_UP -> IN_TRANSIT ->
    ARRIVED before it can be DELIVERED (`DELIVERY_STATUS_TRANSITIONS`). The steps are the real
    writers; `commit=False` leaves out their customer notifications, which no pay test reads, and
    `sync_order_status=False` leaves the order CONFIRMED for the office's DELIVERED. An order with no
    delivery row (held for a later day) is left as it is.
    """
    from business_app.models.order import Order
    from business_app.services.delivery_assignment_service import DeliveryAssignmentService
    from business_app.services.delivery_service import DeliveryService
    from shared.enums import AssignmentSource, DeliveryStatus

    delivery = db.session.get(Order, order_id).delivery
    if delivery is None:
        return
    delivery_id, office_id = delivery.id, office.id
    driver_id = _driver(db).id
    DeliveryAssignmentService.assign_driver(
        delivery_id, driver_user_id=driver_id, actor_id=office_id, source=AssignmentSource.ADMIN_ASSIGN
    )
    service = DeliveryService()
    for status in (DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, DeliveryStatus.ARRIVED):
        service.update_delivery_status(delivery_id, status, driver_id, sync_order_status=False, commit=False)
    db.session.commit()


def make_store(db, *, name: str, workplace: bool = False):
    """A shop's customer account with one address inside the Tashkent polygon.

    An individual account by default: no loyalty tier exists in the test database, so
    `make_order`'s zero-tier-discount precondition holds and every pay figure is the
    line total. `workplace=True` makes a workplace entity, the only kind that may settle
    on the business account. Phones run +99893xxxxxxx, clear of every conftest fixture.
    """
    from business_app.models.user import User, UserAddress
    from shared.enums import EntitySubtype, UserRole, UserType

    _STORE_SEQUENCE["last"] += 1
    number = _STORE_SEQUENCE["last"]
    user = User(
        phone=f"+99893{number:07d}",
        email=f"store-{number}@example.com",
        password_hash="x" * 60,
        first_name=name,
        last_name="Store",
        role=UserRole.CUSTOMER,
        user_type=UserType.ENTITY if workplace else UserType.INDIVIDUAL,
        entity_subtype=EntitySubtype.WORKPLACE if workplace else None,
        company_name=name if workplace else None,
        is_verified=True,
    )
    db.session.add(user)
    db.session.flush()
    db.session.add(
        UserAddress(
            user_id=user.id,
            title="Shop",
            full_address=f"{name}, Tashkent",
            street_address=name,
            city="Tashkent",
            latitude=41.3111,
            longitude=69.2797,
            is_default=True,
        )
    )
    db.session.commit()
    return user


def make_product(db, *, name: str, price: str, size: str = "19L"):
    """A stocked product at `price` (its base price; no discount price)."""
    from business_app.models.product import Product, ProductCategory

    category = ProductCategory.query.filter_by(name="Sales pay test").first()
    if category is None:
        category = ProductCategory(name="Sales pay test", is_active=True)
        db.session.add(category)
        db.session.flush()
    product = Product(
        name=name,
        category_id=category.id,
        size=size,
        base_price=Decimal(price),
        stock_quantity=100000,
        min_stock_level=0,
        max_stock_level=1000000,
        is_active=True,
    )
    db.session.add(product)
    db.session.commit()
    return product


def tier(from_unit: int, mode: str, value) -> dict:
    """One tier as the plan editor posts it (`TierPayload`): its first unit and its rate. The editor
    never sends `to_unit`; A14 derives it (I-29)."""
    return {"from_unit": from_unit, "mode": mode, "value": value}


def version_payload(
    effective_month: str,
    water_product_id: Optional[int] = None,
    *,
    water_rate: int = 1500,
    water_tiers: Optional[List[dict]] = None,
    default_percent: int = 3,
    default_tiers: Optional[List[dict]] = None,
    amount: int = 150000,
    window_days: int = 60,
    min_orders_with_total: int = 2,
    min_combined_total: int = 300000,
    min_orders_any_amount: int = 5,
    lookback_days: int = 180,
) -> dict:
    """The A13 `version` / A15 body as the admin UI posts it: plan "Standard" of §1.2.

    Water 19L on its own schedule: one tier at `water_rate` per unit (v4's flat rate), or the
    `water_tiers` rows (`tier(...)`). It has no schedule when `water_product_id` is None. Every
    other product is on the default schedule: one tier at `default_percent` of its net line, or the
    `default_tiers` rows. Then the default bands (80 / 60 / 0 → ×1.0 / ×0.8 / ×0.5, at least 20
    due) and, by default, a 150,000 new-outlet bonus (60 days; 2 orders ≥ 300,000 or 5 orders;
    180-day lookback). The keywords let a test vary one knob at a time (plan ruling PR7: the one
    copy).
    """
    water = water_tiers if water_tiers is not None else [tier(1, "per_unit", water_rate)]
    return {
        "effective_month": effective_month,
        "default_tiers": default_tiers if default_tiers is not None else [tier(1, "percent", default_percent)],
        "rates": [] if water_product_id is None else [{"product_id": water_product_id, "tiers": water}],
        "gate_bands": [
            {"min_pct": 80, "multiplier": 1},
            {"min_pct": 60, "multiplier": 0.8},
            {"min_pct": 0, "multiplier": 0.5},
        ],
        "gate_min_visits_due": 20,
        "new_outlet_bonus": {
            "amount": amount,
            "window_days": window_days,
            "prior_customer_lookback_days": lookback_days,
            "min_orders_with_total": min_orders_with_total,
            "min_combined_total": min_combined_total,
            "min_orders_any_amount": min_orders_any_amount,
        },
    }


def backdate_delivered(order, at: datetime) -> None:
    """Move the order's DELIVERED history row to `at`.

    The delivered instant is max(order_status_history.changed_at) of the DELIVERED rows
    (I-1), and `update_order_status` stamps it with the wall clock; this is the one
    timestamp a builder rewrites (§10.1).
    """
    from business_app import db as database
    from business_app.models.order import OrderStatusHistory
    from shared.enums import OrderStatus

    rows = OrderStatusHistory.query.filter_by(order_id=order.id, new_status=OrderStatus.DELIVERED).all()
    assert len(rows) == 1, f"order {order.order_number} has {len(rows)} DELIVERED history rows"
    rows[0].changed_at = at
    database.session.commit()


def make_order(
    db,
    *,
    customer,
    lines,
    source: str,
    created_by_staff=None,
    outlet=None,
    delivered_at=None,
    paid_at=None,
    method: str = "cash",
    delivery_fee: Decimal = Decimal("0"),
    discount: Decimal = Decimal("0"),
):
    """One order through the real writers; returns it re-read.

    - `lines`: [(product, quantity)], priced at each product's base price.
    - `delivered_at`: `deliver` it as the office: confirm (unless instant COD or a completed
      business-account payment already did), hand the delivery to the builder driver and bring
      it to the door (`_drive_to_the_door`), mark DELIVERED, then `backdate_delivered`. None
      leaves the order undelivered.
    - `paid_at`: collect whatever is still owed as one standalone office collection with
      `occurred_at=paid_at`, which becomes `Order.paid_at` (I-2). None collects nothing
      (a business-account order is paid at creation; a prepaid-credit order may be too).
    - `delivery_fee`: the flat `DEFAULT_DELIVERY_FEE` a fresh DeliveryService reads once
      (delivery_service.py `calculate_delivery_fee`), set for this call only.
    - `discount`: an absolute order discount, written onto the new order before it is delivered or
      paid. A declared test write, like `backdate_delivered`: `create_order` sets `discount_amount`
      only from a subscription, which an agent order never has, yet the stale absolute discount an
      edit leaves (`order_edit_service._project_totals_after`) is what B3-1 is about.
    """
    from flask import current_app

    from business_app.services.cash_collection_service import CashCollectionService
    from business_app.services.order_service import OrderService
    from business_app.utils.payment_projection import get_payment_projection
    from shared.enums import PaymentMethod

    if method == PaymentMethod.BUSINESS_ACCOUNT.value:
        assert paid_at is None, "a business-account order is paid at creation; there is nothing to collect"
        _workplace_contract(db, customer, [product for product, _quantity in lines])
    address = _delivery_address(customer, outlet)
    office = _office(db)
    order_data = {
        "items": [{"product_id": product.id, "quantity": quantity} for product, quantity in lines],
        "delivery_address": {
            "delivery_address_id": address.id,
            "street": address.street_address,
            "latitude": address.latitude,
            "longitude": address.longitude,
        },
        "payment_method": method,
        "order_source": source,
        "created_by_staff_id": created_by_staff.id if created_by_staff is not None else None,
    }
    saved_fee = current_app.config["DEFAULT_DELIVERY_FEE"]
    current_app.config["DEFAULT_DELIVERY_FEE"] = int(delivery_fee)
    try:
        order = OrderService().create_order(customer.id, order_data)
    finally:
        current_app.config["DEFAULT_DELIVERY_FEE"] = saved_fee
    # The preconditions every hand-computed pay figure rests on.
    assert order.tier_discount == 0, f"tier discount {order.tier_discount} on a pay fixture order"
    assert order.delivery_fee == delivery_fee, f"delivery fee {order.delivery_fee}, expected {delivery_fee}"
    if discount:
        # The columns `create_order` writes for a discounted order: the discount, the total, and the
        # payment row's amount and what is still owed on it.
        order.discount_amount = discount
        order.total_amount = Decimal(order.total_amount) - discount
        if order.payment is not None:
            order.payment.amount = order.total_amount
            order.payment.outstanding_amount = order.total_amount
        db.session.commit()

    if delivered_at is not None:
        deliver(db, order.id, at=delivered_at, actor=office)

    if paid_at is not None:
        db.session.refresh(order)
        owed = get_payment_projection(order.payment)["outstanding_amount"]
        assert owed > 0, f"order {order.order_number} owes nothing to collect"
        CashCollectionService().post_collection(
            customer_id=customer.id,
            amount=owed,
            source="standalone_meeting",
            recorded_by_user_id=office.id,
            order_id=order.id,
            notes="Paid in full at the shop",
            occurred_at=paid_at,
        )
    db.session.refresh(order)
    return order


def make_agent_order(
    db,
    agent,
    outlet,
    lines,
    *,
    delivered_at,
    paid_at,
    method: str = "cash",
    delivery_fee: Decimal = Decimal("0"),
    discount: Decimal = Decimal("0"),
):
    """An order the agent PLACED (C3: `order_source == 'sales_agent'` and
    `created_by_staff_id == agent`) for the outlet's customer account.

    It calls `create_order` directly and never passes through `VisitService.place_order`,
    so it never opens a same-day hold (C14); `place_visit_order` (Task 9) is the builder
    that does.
    """
    from business_app.utils.constants import ORDER_SOURCE_SALES_AGENT

    assert outlet.user is not None, "an agent order is placed for the outlet's customer account"
    return make_order(
        db,
        customer=outlet.user,
        lines=lines,
        source=ORDER_SOURCE_SALES_AGENT,
        created_by_staff=agent,
        outlet=outlet,
        delivered_at=delivered_at,
        paid_at=paid_at,
        method=method,
        delivery_fee=delivery_fee,
        discount=discount,
    )


def _delivery_address(customer, outlet):
    """The outlet's own address when it has one, else the customer's first."""
    from business_app.models.user import UserAddress

    if outlet is not None and outlet.address_id is not None:
        return outlet.address
    address = UserAddress.query.filter_by(user_id=customer.id).order_by(UserAddress.id).first()
    assert address is not None, f"customer {customer.id} has no address; build it with make_store"
    return address


def _workplace_contract(db, customer, products):
    """An active UNITS contract covering every product, priced at the product's own base
    price, so a business-account order totals exactly what the cash order would
    (`CorporateContractService.validate_business_account_order`). Its validity is checked
    against the wall clock at creation, hence a start date of yesterday."""
    from datetime import UTC, timedelta

    from business_app.models.corporate import (
        CorporateContract,
        CorporateContractProductPrice,
        CorporateContractStatus,
        CorporatePrepaymentAccount,
        CorporatePrepaymentBalance,
    )
    from shared.enums import CorporateContractTrackingMode

    contract = CorporateContract.query.filter_by(user_id=customer.id).first()
    if contract is not None:
        return contract
    contract = CorporateContract(
        user_id=customer.id,
        contract_number=f"SP-{customer.id}",
        name=f"{customer.first_name} supply",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1),
        currency="UZS",
        is_active=True,
        tracking_mode=CorporateContractTrackingMode.UNITS,
    )
    db.session.add(contract)
    db.session.flush()
    account = CorporatePrepaymentAccount(contract_id=contract.id, is_active=True)
    db.session.add(account)
    db.session.flush()
    for product in products:
        db.session.add(
            CorporateContractProductPrice(
                contract_id=contract.id,
                product_id=product.id,
                unit_price=product.base_price,
                is_prepayment_eligible=True,
                is_active=True,
            )
        )
        db.session.add(
            CorporatePrepaymentBalance(
                account_id=account.id,
                product_id=product.id,
                prepaid_units=Decimal("500.00"),
                reserved_units=Decimal("0.00"),
                consumed_units=Decimal("0.00"),
                is_active=True,
            )
        )
    db.session.commit()
    return contract


# The Postgres pay world (plan ruling PR5): Task 5's Postgres proofs and Task 7's
# close-race test both build it from here, never from each other's test modules.
PG_SETUP = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)  # 1 October 09:00 local
PG_DELIVERED = datetime(2026, 10, 4, 5, 12, tzinfo=timezone.utc)
PG_PAID = datetime(2026, 10, 4, 5, 20, tzinfo=timezone.utc)


def pay_on_postgres(pg_db) -> Tuple[User, Product]:
    """Plan "Standard" v1 from October, one agent on it, pay started in October. The plan
    goes through the service with the payload `validated_json_payload` would hand A13.
    Returns `(agent, water)`."""
    import os

    from flask import current_app

    from business_app.models.sales import SalesAgentProfile
    from business_app.serializers.sales_pay_serializers import VersionPayload
    from business_app.services.sales.pay_period_service import SalesPayPeriodService
    from business_app.services.sales.pay_plan_service import SalesPayPlanService
    from business_app.services.sales.pay_terms_service import SalesPayTermsService
    from shared.enums import UserRole, UserType
    from tests.unit.test_sales_agent_role import make_sales_agent_user

    october = date(2026, 10, 1)  # the month PG_SETUP falls in
    # `pg_app` is built without the suite's REDIS_URL (TestingConfig falls back to localhost), and
    # `create_order` reserves stock in Redis: point it at the suite's test Redis DB.
    current_app.config["REDIS_URL"] = os.environ["REDIS_URL"]
    admin = User(
        phone="+998901234568",
        email="admin@example.com",
        password_hash="x" * 60,
        first_name="Admin",
        last_name="User",
        user_type=UserType.STAFF,
        role=UserRole.ADMIN,
        is_verified=True,
    )
    pg_db.session.add(admin)
    pg_db.session.commit()
    agent = make_sales_agent_user(pg_db, staff_roles=["sales_agent"])
    pg_db.session.add(SalesAgentProfile(user_id=agent.id, districts=["chilanzar"]))
    pg_db.session.commit()
    water = make_product(pg_db, name="Water 19L", price="20000", size="19L")
    version = VersionPayload.model_validate(version_payload("2026-10", water.id)).model_dump(exclude_unset=True)
    plan = SalesPayPlanService.create_plan("Standard", version, actor_id=admin.id, now=PG_SETUP)
    SalesPayTermsService.set_employment(agent.id, start=october, end=None, actor_id=admin.id, now=PG_SETUP)
    SalesPayTermsService.add_terms(
        agent.id,
        effective_month=october,
        base_salary=Decimal("5000000"),
        plan_id=plan.id,
        note=None,
        actor_id=admin.id,
        now=PG_SETUP,
    )
    SalesPayPeriodService.start(october, is_shadow=False, actor_id=admin.id, now=PG_SETUP)
    return agent, water


def pg_agent_order(pg_db, agent, water, name):
    """Six waters `agent` placed for a new shop `name`: delivered at PG_DELIVERED, paid in
    cash at PG_PAID."""
    outlet = make_outlet(pg_db, onboarded_by=agent, created_at=PG_SETUP, user=make_store(pg_db, name=name))
    return make_agent_order(pg_db, agent, outlet, [(water, 6)], delivered_at=PG_DELIVERED, paid_at=PG_PAID)


# ---- Task 9: the same-day hold ----

_SALES_OUTLETS = "/api/v1/staff/sales/outlets"
_SALES_VISITS = "/api/v1/staff/sales/visits"
APPROVALS = "/api/v1/admin/sales/order-approvals"


def place_visit_order(
    client,
    agent_headers,
    outlet,
    items,
    method,
    *,
    delivery_date: Optional[str] = None,
    checkin: Optional[dict] = None,
) -> dict:
    """One agent order through the real visit routes, exactly as the staff bot walks them.

    start -> check-in -> an empty shelf count -> order -> close. `items` is the bot's
    `[{"product_id", "quantity"}]`, `method` its rail ("cash" / "business_account"), and
    `delivery_date` an optional "YYYY-MM-DD" (the bot's day button); without it the order is
    undated, i.e. today's. The check-in is skipped (`{"skipped": True}`) unless `checkin` is
    given, in which case that body is posted to `/checkin` as is (T-E2E-1 checks in for real at
    the store's pin, PR15). The visit is closed at the end, so the same agent can start the next
    visit at the same outlet. This is the builder that goes through `VisitService.place_order`,
    so it is the one that opens a same-day hold (C14).

    Returns the order route's `data`: `{order, confirmation, visit}`.
    """
    started = client.post(f"{_SALES_OUTLETS}/{outlet.id}/visits", headers=agent_headers)
    assert started.status_code == 201, started.get_data(as_text=True)
    visit_id = started.get_json()["data"]["visit"]["id"]
    for path, body in (
        (f"{_SALES_VISITS}/{visit_id}/checkin", checkin if checkin is not None else {"skipped": True}),
        (f"{_SALES_VISITS}/{visit_id}/stock-check", {"items": []}),
    ):
        response = client.post(path, json=body, headers=agent_headers)
        assert response.status_code == 200, response.get_data(as_text=True)
    payload = {"items": items, "payment_method": method}
    if delivery_date is not None:
        payload["delivery_date"] = delivery_date
    placed = client.post(f"{_SALES_VISITS}/{visit_id}/order", json=payload, headers=agent_headers)
    assert placed.status_code == 201, placed.get_data(as_text=True)
    # No outcome: an order exists, so the backend records `order_placed` itself.
    closed = client.post(f"{_SALES_VISITS}/{visit_id}/close", json={}, headers=agent_headers)
    assert closed.status_code == 200, closed.get_data(as_text=True)
    return placed.get_json()["data"]


# --------------------------------------------------------------------------- #
# Task 6: one name per time concept, and the new-outlet bonus world.
#
# `PAY_API`, `TASHKENT`, `local_at` and `utc_at` are defined once, here (plan ruling PR6).
# `local_at` is a Tashkent wall-clock moment: what `freeze_local_now` and the services'
# `now=` take. `utc_at` is the same moment as the UTC instant the order writers store.
# Tashkent is UTC+5 all year, so `utc_at(y, m, d)` (10:00 local) is 05:00Z.
#
# The bonus world: pay started in September 2026 under plan "Standard" v1, grocery-store
# customers, outlets a manager activated through the real `OutletService.approve`, and
# orders delivered and paid through the real writers with only their instants backdated.
# `collect_cash_at` is the backdating cash writer; Task 7's `collect_cash` is the admin
# collections route on the wall clock.
# --------------------------------------------------------------------------- #

PAY_API = "/api/v1/admin/sales/pay"
TASHKENT = ZoneInfo(DISPLAY_TIMEZONE)


def local_at(year: int, month: int, day: int, hour: int = 10, minute: int = 0) -> datetime:
    """A Tashkent wall-clock moment: what `freeze_local_now` and the services' `now=` take."""
    return datetime(year, month, day, hour, minute, tzinfo=TASHKENT)


def utc_at(year: int, month: int, day: int, hour: int = 10, minute: int = 0) -> datetime:
    """The same wall-clock moment as a UTC instant: what the order writers are given."""
    return local_at(year, month, day, hour, minute).astimezone(timezone.utc)


BONUS_EPOCH = date(2026, 9, 1)
BONUS_PAY_START = utc_at(2026, 9, 1, 9)
BONUS_UNIT_PRICE = Decimal("10000.00")


def start_pay(client, admin_user, admin_headers, *agents):
    """Pay starts in September 2026 under plan "Standard" v1, every agent employed from 1 September.

    Once pay has started, the earliest open month (September) is editable whatever the wall clock
    says, so A13 needs no frozen clock. Returns (plan_id, version_id)."""
    from business_app.services.sales.pay_period_service import SalesPayPeriodService
    from business_app.services.sales.pay_terms_service import SalesPayTermsService

    SalesPayPeriodService.start(BONUS_EPOCH, is_shadow=False, actor_id=admin_user.id, now=BONUS_PAY_START)
    created = client.post(
        f"{PAY_API}/plans", json={"name": "Standard", "version": version_payload("2026-09")}, headers=admin_headers
    )
    assert created.status_code == 201, created.get_data(as_text=True)
    plan = created.get_json()["data"]["plan"]
    for agent in agents:
        SalesPayTermsService.set_employment(
            agent.id, start=BONUS_EPOCH, end=None, actor_id=admin_user.id, now=BONUS_PAY_START
        )
        SalesPayTermsService.add_terms(
            agent.id,
            effective_month=BONUS_EPOCH,
            base_salary=Decimal("5000000"),
            plan_id=plan["id"],
            note=None,
            actor_id=admin_user.id,
            now=BONUS_PAY_START,
        )
    return plan["id"], plan["versions"][0]["id"]


def sync_agent(agent, now):
    """One run of the ledger sync for one agent at `now`. A failure or a key conflict would hide
    in the stats, so both are asserted away here."""
    from business_app.services.sales.pay_ledger_service import SalesPayLedgerService

    stats = SalesPayLedgerService.sync([agent.id], now=now)
    assert stats.failed == [] and stats.conflicts == 0, stats.as_dict()
    return stats


def grocery_store(db, phone, name):
    """A grocery-store customer: an entity, so no tier discount reaches an order's total."""
    from shared.enums import EntitySubtype
    from tests.unit.test_outlet_dedupe import _customer

    return _customer(db, phone, company=name, subtype=EntitySubtype.GROCERY_STORE)


def approved_outlet(db, *, agent, operator, customer, onboarded_at, pin):
    """An outlet `agent` onboarded at `onboarded_at`, activated by `operator` through the real
    `OutletService.approve`: the first `active` row is `approved`, and its actor is the operator."""
    from business_app.services.sales.outlet_service import OutletService

    outlet = make_outlet(
        db, onboarded_by=agent, created_at=onboarded_at, user=customer, pin=pin, stage="activation_requested"
    )
    OutletService.approve(outlet.id, actor_id=operator.id)
    outlet = db.session.get(Outlet, outlet.id)
    assert outlet.stage == "active" and outlet.address_id is not None
    return outlet


def outlet_order(db, customer, outlet, units, product, *, delivered_at, paid_at, source="telegram", staff=None):
    """One order of `units` x `product` to `outlet` through Task 5's `make_order`; it totals
    `units` x BONUS_UNIT_PRICE (no delivery fee, no tier discount)."""
    placed = make_order(
        db,
        customer=customer,
        lines=[(product, units)],
        source=source,
        created_by_staff=staff,
        outlet=outlet,
        delivered_at=delivered_at,
        paid_at=paid_at,
    )
    assert placed.total_amount == BONUS_UNIT_PRICE * units
    return placed


def deliver(db, order_id, *, at, actor):
    """Deliver an order with the real status writers, as `actor`, and backdate only its DELIVERED
    history row to `at`. The one delivery walk of the builders: `make_order` delivers through it.

    An order still PENDING (the store's question) is confirmed first, as an operator would. The
    DELIVERED writer completes the order's delivery, which needs a driver at the door, so the
    delivery is walked there first (`_drive_to_the_door`)."""
    from business_app.models.order import Order
    from business_app.services.order_service import OrderService
    from shared.enums import OrderStatus

    service = OrderService()
    actor_id = actor.id
    if db.session.get(Order, order_id).status == OrderStatus.PENDING:
        service.update_order_status(order_id, OrderStatus.CONFIRMED, updated_by=actor_id)
    _drive_to_the_door(db, order_id, actor)
    service.update_order_status(order_id, OrderStatus.DELIVERED, updated_by=actor_id)
    delivered = db.session.get(Order, order_id)
    backdate_delivered(delivered, at)
    return delivered


def collect_cash_at(db, order, *, at, actor):
    """Pay a delivered order through the cash writer the builders use, backdated: `occurred_at`
    becomes `orders.paid_at` (I-2). Not a route (Task 7's `collect_cash` is the route)."""
    from business_app.services.cash_collection_service import CashCollectionService
    from business_app.utils.timezone_utils import ensure_utc

    CashCollectionService().post_collection(
        customer_id=order.user_id,
        amount=order.total_amount,
        source="standalone_meeting",
        recorded_by_user_id=actor.id,
        order_id=order.id,
        notes="Collected at the counter",
        occurred_at=at,
    )
    db.session.refresh(order)
    assert order.is_paid is True and ensure_utc(order.paid_at) == at
    return order


def place_two_same_day_orders(client, headers, outlet, product, units=(20, 15)):
    """Two orders at one outlet on one local day through the real visit routes; the second waits
    for a manager (C14). Both land on the wall clock's day, which is the day the rule reads."""
    first = place_visit_order(client, headers, outlet, [{"product_id": product.id, "quantity": units[0]}], "cash")
    second = place_visit_order(client, headers, outlet, [{"product_id": product.id, "quantity": units[1]}], "cash")
    assert first["confirmation"]["state"] != "awaiting_staff_approval"
    assert second["confirmation"]["state"] == "awaiting_staff_approval"
    return first["order"]["id"], second["order"]["id"]


@pytest.fixture
def water(db, sample_category):
    """The bonus world's product: BONUS_UNIT_PRICE a unit, stock enough for every scenario.
    Import it with `# noqa: F401 -- a fixture`."""
    product = Product(
        name="Water 5L",
        category_id=sample_category.id,
        size="5L",
        base_price=BONUS_UNIT_PRICE,
        stock_quantity=100000,
        is_active=True,
        in_sales_stock_check=True,
    )
    db.session.add(product)
    db.session.commit()
    return product


def admin_agent(app, db, phone):
    """An ADMIN who also holds a sales-agent profile, with one token for the staff and admin routes."""
    from flask_jwt_extended import create_access_token

    from business_app.models.sales import SalesAgentProfile
    from shared.enums import UserRole
    from tests.unit.test_sales_agent_role import make_sales_agent_user

    user = make_sales_agent_user(db, phone=phone, role=UserRole.ADMIN, staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=user.id, districts=["chilanzar"]))
    db.session.commit()
    with app.app_context():
        token = create_access_token(identity=str(user.id), additional_claims={"role": "admin"})
    return user, {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Task 7: pay configured through the admin pay routes, and the clock moved with it.
#
# `PAY_API`, `TASHKENT`, `local_at` and `utc_at` are Task 6's names (its section above);
# this section uses them and never redefines them. The month constants, the carry-case
# calendar and the statement/carry readers below are the names Task 8 and Task 10 read too;
# they are defined once, here.
# ---------------------------------------------------------------------------

OCT, NOV, DEC = date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1)

# The §1.2 carry case's October: 1 October is a holiday and every working day but the 2nd and
# the 3rd is unpaid, so a base of 3,000,000 pays 3,000,000 x 2 / 30 = 200,000. Every day is a
# working day (C2 v5): October 2026 has 31, 30 after the holiday, and 4-31 October are unpaid.
TEACHERS_DAY = {"days": [{"date": "2026-10-01", "note": "Teachers' Day"}]}
CARRY_CASE_UNPAID = [f"2026-10-{d:02d}" for d in range(4, 32)]
# Every November 2026 working day (no holiday): all 30.
NOVEMBER_WORKING = [f"2026-11-{d:02d}" for d in range(1, 31)]


def frozen_statements(agent_id) -> Dict[date, Any]:
    """The agent's frozen statements by month, re-read."""
    from business_app.models.sales_pay import SalesPayStatement

    SalesPayStatement.query.session.expire_all()
    return {s.period.month_start: s for s in SalesPayStatement.query.filter_by(agent_user_id=agent_id).all()}


def carry_rows() -> List[Any]:
    """Every carry-forward adjustment approve has written."""
    from business_app.models.sales_pay import SalesPayAdjustment

    return SalesPayAdjustment.query.filter_by(source="carry_forward").all()


def pay_agent(db, *, phone: str, first_name: str, role=None):
    """A user holding the sales_agent staff role and an active profile with no employment yet.

    `role=UserRole.ADMIN` is §4.13's admin who is also an agent. The account is ACTIVE, so
    `super_admin_required` lets that admin's own token through.
    """
    from business_app.models.sales import SalesAgentProfile
    from shared.enums import UserRole, UserStatus
    from tests.unit.test_sales_agent_role import make_sales_agent_user

    user = make_sales_agent_user(db, phone=phone, role=role or UserRole.SALES_AGENT, staff_roles=["sales_agent"])
    user.first_name = first_name
    user.status = UserStatus.ACTIVE.value
    db.session.add(SalesAgentProfile(user_id=user.id, districts=["chilanzar"]))
    db.session.commit()
    return user


def pay_call(client, method: str, path: str, headers, payload=None, *, status: int = 200):
    """One HTTP call with its status asserted. Returns `data` on success, the error body otherwise."""
    kwargs = {"headers": headers}
    if method != "GET":
        kwargs["json"] = {} if payload is None else payload
    response = client.open(path, method=method, **kwargs)
    body = response.get_json()
    assert response.status_code == status, (method, path, response.status_code, body)
    return body["data"] if status < 400 else body


def pay_route(world, method: str, path: str, payload=None, *, status: int = 200):
    """`pay_call` on the admin pay blueprint, as the world's admin."""
    return pay_call(world.client, method, f"{PAY_API}{path}", world.headers, payload, status=status)


def configure_agent(client, headers, agent, *, plan_id: int, base: int, effective_month: str,
                    employment_start: str, employment_end=None) -> Dict[str, Any]:
    """A18 then A17, in the Pay tab's order: terms are refused while the employment start is
    unset. Returns A17's A16-shaped answer."""
    pay_call(client, "PUT", f"{PAY_API}/agents/{agent.id}/employment", headers,
             {"start": employment_start, "end": employment_end})
    return pay_call(
        client,
        "POST",
        f"{PAY_API}/agents/{agent.id}/terms",
        headers,
        {"effective_month": effective_month, "base_salary": str(base), "plan_id": plan_id},
        status=201,
    )


def pay_world(db, client, monkeypatch, admin, headers, agent, *, start: str = "2026-10", rate_19l: int = 1500,
              base: int = 3000000, is_shadow: bool = False, employment_start=None,
              employment_end=None) -> SimpleNamespace:
    """Pay configured the way an admin configures it, at 09:00 on the first day of `start`.

    A13 plan "Standard" v1 effective `start` (19 L at `rate_19l` per unit, everything else 3 %,
    the default bands, 20 visits due), A18 employment (from the 1st unless told), A17 terms on
    `base`, A0 start; plus the agent's store, onboarded a month earlier. The products are a
    19 L water at 20,000 and a 1 L juice at 25,000 (stored in the 5L size: `product_size_enum`
    has no 1L, and only the water carries a product rate). Every later step moves the clock with
    `move_clock`.
    """
    year, month = (int(part) for part in start.split("-"))
    freeze_local_now(monkeypatch, local_at(year, month, 1, 9))
    products = SimpleNamespace(
        water=make_product(db, name="Water 19L", price="20000", size="19L"),
        juice=make_product(db, name="Juice 1L", price="25000", size="5L"),
    )
    plan = pay_call(
        client,
        "POST",
        f"{PAY_API}/plans",
        headers,
        {"name": "Standard", "version": version_payload(start, products.water.id, water_rate=rate_19l)},
        status=201,
    )["plan"]
    configure_agent(
        client,
        headers,
        agent,
        plan_id=plan["id"],
        base=base,
        effective_month=start,
        employment_start=employment_start or f"{start}-01",
        employment_end=employment_end,
    )
    pay_call(client, "POST", f"{PAY_API}/start", headers, {"month": start, "is_shadow": is_shadow}, status=201)
    return SimpleNamespace(
        db=db, client=client, monkeypatch=monkeypatch, admin=admin, headers=headers, agent=agent,
        plan=plan, products=products, store=pay_store(db, agent, name="Oasis market", onboarded=local_at(year, month, 1, 9)),
    )


def pay_store(db, agent, *, name: str, onboarded: datetime):
    """A shop `agent` onboarded a month before `onboarded`, on its own customer account (Task 5's
    `make_store`, so an order to it carries no tier discount)."""
    return make_outlet(
        db,
        onboarded_by=agent,
        created_at=onboarded.astimezone(timezone.utc) - timedelta(days=30),
        user=make_store(db, name=name),
        assigned_agent=agent,
    )


def move_clock(world, when: datetime) -> datetime:
    """Freeze every clock the pay services read at `when` (a `local_at` moment)."""
    return freeze_local_now(world.monkeypatch, when)


def water_order(world, bottles: int, day: date, *, at=(10, 0), paid: bool = True, agent=None, store=None):
    """An agent order of `bottles` x 19 L for the world's store (or `store`), delivered at `at`
    local time on `day` and, unless `paid=False`, paid that same minute. The clock is moved to
    08:00 that day first, so the order is placed on the day it is delivered."""
    move_clock(world, local_at(day.year, day.month, day.day, 8, 0))
    delivered = utc_at(day.year, day.month, day.day, *at)
    return make_agent_order(
        world.db,
        agent or world.agent,
        store or world.store,
        [(world.products.water, bottles)],
        delivered_at=delivered,
        paid_at=delivered if paid else None,
    )


def publish_version(
    world, effective_month: str, *, rate_19l: Optional[int] = None, tiers_19l: Optional[List[dict]] = None
) -> Dict[str, Any]:
    """A15 on the world's plan, with 19 L on one flat rate (`rate_19l`, a single tier) or on
    `tiers_19l` (`tier(...)` rows). Returns the `VersionDetail`."""
    if (rate_19l is None) == (tiers_19l is None):
        raise TypeError("publish_version takes exactly one of rate_19l and tiers_19l")
    return pay_route(
        world,
        "POST",
        f"/plans/{world.plan['id']}/versions",
        version_payload(effective_month, world.products.water.id, water_rate=rate_19l, water_tiers=tiers_19l),
        status=201,
    )["version"]


def below_zero_october(world) -> None:
    """§1.2's carry case with an admin deduction in place of its penalty (penalties are Task 8's).

    A7 holiday on 1 October, A10 marks 28 of the 30 working days unpaid (3,000,000 x 2 / 30 =
    200,000), A11 deducts 300,000: variable -300,000, gross -100,000, paid 0. Needs a world built
    with `base=3000000`.
    """
    agent = world.agent.id
    pay_route(world, "PUT", "/periods/2026-10/holidays", TEACHERS_DAY)
    pay_route(world, "PUT", f"/periods/2026-10/agents/{agent}/unpaid-days",
              {"days": [{"date": day} for day in CARRY_CASE_UNPAID]})
    pay_route(world, "POST", f"/periods/2026-10/agents/{agent}/adjustments",
              {"amount": -300000, "reason": "Recovered stock shortfall"}, status=201)


def ledger_lines(order) -> List[Any]:
    """The order's pay lines in posting order, re-read after the routes' sessions wrote them."""
    from business_app import db as database
    from business_app.models.sales_pay import SalesPayLedgerLine

    database.session.expire_all()
    return SalesPayLedgerLine.query.filter_by(order_id=order.id).order_by(SalesPayLedgerLine.id).all()


def live_cash_events(order) -> List[Any]:
    """The order's un-voided collections that still carry money, oldest first."""
    from business_app import db as database
    from business_app.models.payment import CashCollectionEvent

    database.session.expire_all()
    return (
        CashCollectionEvent.query.filter(
            CashCollectionEvent.order_id == order.id,
            CashCollectionEvent.voided_at.is_(None),
            CashCollectionEvent.amount > 0,
        )
        .order_by(CashCollectionEvent.id)
        .all()
    )


def adjust_cash(world, event_id: int, new_amount: int) -> int:
    """The super-admin correction, POST .../cash-reconciliation/events/<id>/adjust. Returns the
    replacement event's id, which the next correction of the same money must target."""
    data = pay_call(
        world.client,
        "POST",
        f"/api/v1/admin/staff/cash-reconciliation/events/{event_id}/adjust",
        world.headers,
        {"new_amount": new_amount, "reason": "Collected cash corrected by the office"},
    )
    return data["cash_collection_event"]["id"]


def collect_cash(world, order, amount: int) -> int:
    """Money for the order recorded at the office, POST .../cash-reconciliation/collections, with
    the payload the admin UI sends (no `occurred_at`, so the wall clock stamps it). A re-credit
    keeps the first credit's month (I-19), so that stamp never moves a line."""
    data = pay_call(
        world.client,
        "POST",
        "/api/v1/admin/staff/cash-reconciliation/collections",
        world.headers,
        {
            "customer_id": order.user_id,
            "amount": amount,
            "order_id": order.id,
            "source": "standalone_meeting",
            "notes": "Balance paid at the office",
        },
        status=201,
    )
    return data["cash_collection_event"]["id"]


def plan_days(db, agent, days, outlet_ids) -> None:
    """A frozen due set of `outlet_ids` on each of `days` (Task 2's builder)."""
    for day in days:
        make_day_plan(db, agent, day, list(outlet_ids))


def verified_visits(db, agent, days, outlet_ids, *, seconds: int = 120) -> None:
    """One verified visit (completed, checked in, in range, `seconds` long) per outlet per day."""
    for day in days:
        started = utc_at(day.year, day.month, day.day, 11)
        for outlet_id in outlet_ids:
            make_visit(
                db,
                agent_user_id=agent.id,
                outlet_id=outlet_id,
                status="completed",
                planned=True,
                started_at=started,
                checkin_at=started,
                ended_at=started + timedelta(seconds=seconds),
                in_radius=True,
                checkin_skipped=False,
            )


# ---------------------------------------------------------------------------
# Task 8: penalties, the pay pushes, the carry case
# ---------------------------------------------------------------------------

PENALTY_TYPES_URL = f"{PAY_API}/penalty-types"
PENALTIES_URL = f"{PAY_API}/penalties"
PROPOSALS_URL = "/api/v1/admin/sales/penalty-proposals"

# A20 requires all three: agents read a penalty type's name in their own language.
PENALTY_TYPE_NAMES = {
    "en": "Missed planned visits",
    "uz": "Rejadagi tashriflar o'tkazib yuborildi",
    "ru": "Пропущенные плановые визиты",
}
# Illustration B's unpaid days, Wednesday 14 and Thursday 15 October 2026 (A10's body).
ILLUSTRATION_B_UNPAID = {"days": [{"date": "2026-10-14", "note": "Sick"}, {"date": "2026-10-15", "note": "Sick"}]}


def refusal(client, method: str, path: str, headers, payload=None, *, status: int, code: str) -> Dict[str, Any]:
    """`pay_call` for one coded refusal: the status and `error_code` asserted, `details` returned.

    `handle_api_exception` adds `validation_errors: []` to the details of every ValidationError,
    so a test that compares a 400's details compares that key as well.
    """
    body = pay_call(client, method, path, headers, payload, status=status)
    assert body.get("error_code") == code, body
    return body.get("details") or {}


def claim_headers(app, user, role: str) -> Dict[str, str]:
    """Headers whose token carries `role` as its CLAIM, which the admin decorators read, for a user
    no conftest fixture covers: a second admin, or an admin or manager who holds an agent profile."""
    with app.app_context():
        token = create_access_token(identity=str(user.id), additional_claims={"role": role})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def staff_agent(
    db, *, phone: str, first_name: str, role: UserRole = UserRole.SALES_AGENT, telegram_id: Optional[str] = None
) -> User:
    """Task 7's `pay_agent`, with the `users.role` and the chat a Task 8 scenario needs.

    `role=UserRole.ADMIN` or `UserRole.MANAGER` is §4.13's dual-role cast (Anvar, Mansur): the
    profile makes the user a subject of pay decisions, and the role decides whether they may take
    one about themselves. `telegram_id` gives the pay pushes somewhere to go.
    """
    user = pay_agent(db, phone=phone, first_name=first_name, role=role)
    user.telegram_id = telegram_id
    db.session.commit()
    return user


def incident_body(agent, penalty_type_id: int, incident_date: str, **extra) -> Dict[str, Any]:
    """M2's body, and A23's with `amount=` in `extra`: what the penalty forms post."""
    return {
        "agent_user_id": agent.id,
        "penalty_type_id": penalty_type_id,
        "incident_date": incident_date,
        "reason": "Did not visit 5 planned outlets",
        "evidence": "Route sheet and check-ins of the day",
        **extra,
    }


def add_penalty_type(world, *, default_amount: int, names: Optional[Dict[str, str]] = None) -> int:
    """A20 as the world's admin. Returns the new type's id."""
    body = {"names": names or PENALTY_TYPE_NAMES, "default_amount": default_amount}
    return pay_route(world, "POST", "/penalty-types", body, status=201)["type"]["id"]


def spy_sales_pushes(
    monkeypatch, trail: Optional[List[str]] = None, *, fail_for: Sequence[int] = ()
) -> List[Tuple[tuple, dict]]:
    """Record every `push_sales_event.delay`, bound against the TASK's own signature.

    `Task.delay(*args, **kwargs)` binds anything, so the binding is against `run` (the task
    function with `self` bound, `bind=True`): a call the task would reject raises here. It is
    patched through the instance `__dict__` (`_patch_task_publish`), never `setattr`, so no stale
    no-op outlives the test. Each call appends "push" to `trail`: with a commit listener on the
    same list, that proves the push came after the commit that wrote its row.

    A push to a chat in `fail_for` raises the broker's own `OperationalError`, as `.delay` does
    when Redis is unreachable, and is not recorded: it was never queued.
    """
    from kombu.exceptions import OperationalError

    from business_app.tasks import sales_agent_tasks

    task = sales_agent_tasks.push_sales_event
    signature = inspect.signature(task.run)
    calls: List[Tuple[tuple, dict]] = []

    def fake(*args, **kwargs):
        signature.bind(*args, **kwargs)
        if args[0] in fail_for:
            raise OperationalError("Error 111 connecting to redis:6379. Connection refused.")
        calls.append((args, kwargs))
        if trail is not None:
            trail.append("push")

    monkeypatch.setitem(vars(task), "delay", fake)
    return calls


@contextmanager
def logged_push_failures(caplog) -> Iterator[List[logging.LogRecord]]:
    """The exceptions `services/sales/notifications` logs while the block runs; the list is filled
    when the block exits.

    Its logger propagates into a `business_app.*` logger with `propagate=False`
    (business_app/utils/logging_config.py), so caplog's root handler never sees the record; the
    handler is attached to the module's own logger instead (the photo-proxy test's pattern).
    """
    from business_app.services.sales import notifications

    records: List[logging.LogRecord] = []
    notifications.logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.ERROR, logger=notifications.logger.name):
            yield records
            records.extend(r for r in caplog.records if r.name == notifications.logger.name and r.exc_info)
    finally:
        notifications.logger.removeHandler(caplog.handler)


def pushes_of(calls, event: str) -> List[Tuple[int, Dict[str, Any], Optional[str]]]:
    """`(telegram_id, payload, event_id)` of each recorded push of `event`, in publish order."""
    return [(args[0], args[2], kwargs.get("event_id")) for args, kwargs in calls if args[1] == event]


def statement_id(world, month: str, agent) -> int:
    """The id of `agent`'s frozen statement in `month`, as A2 publishes it (A8 carries none)."""
    detail = pay_route(world, "GET", f"/periods/{month}")
    return next(row["statement_id"] for row in detail["agents"] if row["agent_user_id"] == agent.id)


def october_world(db, client, monkeypatch, admin, headers, agent, *, telegram_id: str = "777000201") -> SimpleNamespace:
    """Illustration B's October (§1.2) through the admin routes, at 10:00 on Thursday 15 October 2026.

    Task 7's `pay_world`: pay starts in October (not a shadow month), plan "Standard", base
    3,000,000, employed from the 1st; here 19 L pays 1,000 per unit. A7's holiday on the 1st and
    A10's unpaid 14th and 15th leave 28 of 30 working days: 3,000,000 x 28 / 30 = 2,800,000. The agent's
    chat is set, so the pushes have somewhere to go, and one penalty type defaults to 150,000.
    """
    world = pay_world(db, client, monkeypatch, admin, headers, agent, rate_19l=1000)
    agent.telegram_id = telegram_id
    db.session.commit()
    pay_route(world, "PUT", "/periods/2026-10/holidays", TEACHERS_DAY)
    pay_route(world, "PUT", f"/periods/2026-10/agents/{agent.id}/unpaid-days", ILLUSTRATION_B_UNPAID)
    world.penalty_type_id = add_penalty_type(world, default_amount=150000)
    move_clock(world, local_at(2026, 10, 15))
    return world


def carry_case(client, admin_headers, *, leaver: bool, shadow: bool = False, monkeypatch, admin) -> SimpleNamespace:
    """§1.2's carry case through the admin routes, left open at 12:00 on Saturday 3 October 2026.

    Aziz's base is 3,000,000 (Illustration B's base and calendar, spec E.2.5 item 4), and 19 L pays
    1,000 per unit (the spec prices it at 1,500; at 1,000 every T-OWED figure is a whole number of
    bottles). A7's holiday on the 1st and A10's 28 unpaid days leave the 2nd and the 3rd worked:
    3,000,000 x 2 / 30 = 200,000.

    - `shadow=False`: pay starts in September. 300 x 19 L delivered and paid on 15 September is
      credited 300,000, and September is closed and approved on 2 October at x1.0 (nothing was
      due). On 3 October 30 x 19 L is delivered and paid (30,000), the September order's cash is
      corrected to 0 (the admin's Sync now posts a -300,000 reversal into October, late, gated at
      September's x1.0), and A23 books a 30,000 penalty at the type's default. October then reads
      variable 30,000 - 300,000 - 30,000 = -300,000 and gross 200,000 - 300,000 = -100,000.
    - `shadow=True`: October is the shadow epoch, so there is no earlier credit, and A23 books a
      300,000 penalty: gross 200,000 - 300,000 = -100,000 again.
    - `leaver=True`: employment ends on 31 October, so the shortfall is owed, not carried (I-26).

    Returns Task 7's world, plus `penalty_type_id`, `penalty_id`, `september` and `october` (the
    two orders, or None) and `telegram_id`. September's approval push goes to the conftest no-op;
    a test spies after this returns. T-CARRY-*, T-OWED-* and Task 10's S1 halves build on it.

    Two fixture facts the scenario needs, neither a pay rule:
    - one order line takes at most `MAX_QUANTITY_PER_ITEM` (100) units, an abuse guard at
      placement. It is widened for this test only, so September stays ONE order (one credit, one
      reversal line) and later scenarios can book a month's bottles as one order too;
    - once September's cash is corrected, the "Oasis market" account owes its 300,000 again, and
      the cash writer settles a customer's OLDEST delivered debt first, whatever order a
      collection names. `world.store` therefore becomes a second shop of Aziz's, "Chorsu", so a
      later `water_order` is paid itself and never settles September behind the scenario's back.
    """
    from flask import current_app

    from business_app import db  # the `db` fixture's own object; the module has no module-level `db`

    monkeypatch.setitem(current_app.config, "MAX_QUANTITY_PER_ITEM", 1000)
    agent = staff_agent(db, phone="+998901238301", first_name="Aziz", telegram_id="777000301")
    world = pay_world(
        db,
        client,
        monkeypatch,
        admin,
        admin_headers,
        agent,
        start="2026-10" if shadow else "2026-09",
        rate_19l=1000,
        base=3000000,
        is_shadow=shadow,
        employment_end="2026-10-31" if leaver else None,
    )
    world.penalty_type_id = add_penalty_type(world, default_amount=300000 if shadow else 30000)
    world.september = world.october = None
    if not shadow:
        world.september = water_order(world, 300, date(2026, 9, 15))
        move_clock(world, local_at(2026, 10, 2, 10))
        pay_route(world, "POST", "/periods/2026-09/close")
        pay_route(world, "POST", "/periods/2026-09/approve")
    pay_route(world, "PUT", "/periods/2026-10/holidays", TEACHERS_DAY)
    pay_route(
        world, "PUT", f"/periods/2026-10/agents/{agent.id}/unpaid-days", {"days": [{"date": day} for day in CARRY_CASE_UNPAID]}
    )
    if not shadow:
        world.october = water_order(world, 30, date(2026, 10, 3))
    move_clock(world, local_at(2026, 10, 3, 12))
    if not shadow:
        adjust_cash(world, live_cash_events(world.september)[0].id, 0)
        world.store = pay_store(db, agent, name="Chorsu", onboarded=local_at(2026, 10, 3, 12))
        # The Pay page's "Sync now": an estimate read syncs an agent at most once per
        # SALES_PAY_ONDEMAND_SYNC_SECONDS, and the reads above already did.
        pay_route(world, "POST", "/periods/2026-10/recalculate")
    penalty = pay_route(world, "POST", "/penalties", incident_body(agent, world.penalty_type_id, "2026-10-03"), status=201)
    world.penalty_id = penalty["penalty"]["id"]
    world.telegram_id = int(agent.telegram_id)
    return world


# --------------------------------------------------------------------------- #
# v5 Task 3: what a commission line counts (levels, OQ-B3)
# --------------------------------------------------------------------------- #

# T-TIER-1's 19 L schedule (Illustration C, §1.2), as the plan editor posts it. The ledger tests
# use it here; Task 4's `illustration_c` builder and its route tests reuse it (one copy).
ILLUSTRATION_C_TIERS = [
    tier(1, "per_unit", 1000),
    tier(501, "per_unit", 1500),
    tier(1001, "per_unit", 2000),
    tier(2001, "per_unit", 2500),
]


def units_applied_of(line) -> Dict[int, int]:
    """The units a credit or difference line counts per product, as the sync froze them (§3.2), with
    the JSON-string product ids turned back into ints. A reversal counts none and has no such key."""
    return {int(product_id): units for product_id, units in line.snapshot["units_applied"].items()}


def level_of(order):
    """The level the ledger counts `order` at now (I-31, I-41): the sync's own reading,
    `OrderCreditState(lines).level` over `counted_level`, never re-derived here. None before the
    first credit or after a reversal."""
    from business_app.services.sales.pay_ledger_service import OrderCreditState

    return OrderCreditState(ledger_lines(order)).level


# --------------------------------------------------------------------------- #
# Task 4 (v5): month-level tiers through the routes
# --------------------------------------------------------------------------- #

# `ILLUSTRATION_C_TIERS` (T-TIER-1's 19 L schedule) is Task 3's, defined above in this module.


def illustration_c(world, *, publish: bool = True) -> SimpleNamespace:
    """§1.2 Illustration C (T-TIER-2): A 300, B 250 and C 150 bottles of 19 L, each for a shop of its
    own (so a cash correction of one never settles another's debt), delivered and paid at 10:00
    local on 5, 12 and 20 October: 6,000,000 / 5,000,000 / 3,000,000. With `publish`, A15 first
    puts T-TIER-1's schedule on October (a same-month version, at 10:00 on 1 October); without it
    the orders sit on the world's own plan (T-TIER-14 publishes the tiers later). Nothing is synced
    here: the first read or "Sync now" does it.

    One order line takes at most `MAX_QUANTITY_PER_ITEM` (100) units, a placement abuse guard and
    not a pay rule; it is widened for the test (`carry_case`'s precedent).
    """
    world.monkeypatch.setitem(world.client.application.config, "MAX_QUANTITY_PER_ITEM", 1000)
    if publish:
        move_clock(world, local_at(2026, 10, 1, 10))
        publish_version(world, "2026-10", tiers_19l=ILLUSTRATION_C_TIERS)
    orders = {}
    for name, bottles, day in (("a", 300, 5), ("b", 250, 12), ("c", 150, 20)):
        shop = pay_store(world.db, world.agent, name=f"Shop {name.upper()}", onboarded=local_at(2026, 10, 1, 9))
        orders[name] = water_order(world, bottles, date(2026, 10, day), store=shop)
    return SimpleNamespace(**orders)


def edit_order_items(world, order, items: List[dict]) -> Dict[str, Any]:
    """The Orders page's edit, POST /api/v1/admin/orders/<id>/edit, with the items as the edit modal
    posts them (`orderItemId`, `productId`, `quantity`; a new line has `orderItemId` None, a removed
    one quantity 0). A delivered order is editable for ORDER_EDIT_WINDOW_HOURS by the WALL clock,
    so the window is widened here: without it every caller would start failing once the real
    calendar passed the fixture's October."""
    world.monkeypatch.setitem(world.client.application.config, "ORDER_EDIT_WINDOW_HOURS", 24 * 3650)
    return pay_call(
        world.client,
        "POST",
        f"/api/v1/admin/orders/{order.id}/edit",
        world.headers,
        {"items": items, "reason": "Customer changed the order"},
    )


def close_and_approve(world, month: str, *, at: Optional[datetime] = None) -> None:
    """A3 then A5 on `month` ("YYYY-MM"), at `at` (a `local_at` moment) when given. The close syncs
    the month first."""
    if at is not None:
        move_clock(world, at)
    pay_route(world, "POST", f"/periods/{month}/close")
    pay_route(world, "POST", f"/periods/{month}/approve")
