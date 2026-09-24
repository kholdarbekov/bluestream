"""PATCH /api/v1/admin/orders/<id>/schedule -- move a delivery to another day, whatever its state.

Spec: docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md (R1-R12, R23, R24, §3.4). Driven
through the endpoint the Orders page calls: what the operator sees is what has to be right, and a
service-level check that a status flipped would still pass if the driver kept the stop.

The matrix builds every delivery the way production leaves it -- a real driver `User` plus
`DeliveryPerson` (`deliveries.delivery_person_id` holds the USER id), a status-history row, the
bottle-session binding a driver's accept creates, and the stop on today's route -- then checks the
whole post-state of each cell, not only the status. Replaced: the clock, and the audit sink (a
recorder bound to the real `AuditLogger.log_event` signature). The post-commit notices are pinned
per cell by the fan-out tests at the bottom, against recorders bound to the real signatures of the
Celery tasks and the staff-bot route-updated webhook they stand in for.
"""

import inspect
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import NamedTuple, Optional
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import text

from business_app import db
from business_app.models.bottle import DriverBottleSession, DriverBottleSessionOrder
from business_app.models.corporate import CorporateContract, CorporateContractStatus
from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute, DeliveryStatusHistory
from business_app.models.notification import Notification
from business_app.models.order import Order, OrderStatusHistory
from business_app.models.user import User
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.utils.audit_logger import AuditEventType, AuditLogger, AuditSeverity, audit_logger
from business_app.utils.constants import NotificationChannel, NotificationStatus, NotificationType
from business_app.utils.password_security import hash_password
from shared.enums import (
    CorporateContractTrackingMode,
    DeliveryStatus,
    DriverBottleSessionStatus,
    EntitySubtype,
    OrderStatus,
    PaymentMethod,
    UserRole,
    UserType,
)

S = DeliveryStatus
TZ = ZoneInfo("Asia/Tashkent")
SCHEDULE_URL = "/api/v1/admin/orders/{}/schedule"
REASON = "Mijoz boshqa kunni so'radi"
DRIVER_TELEGRAM_ID = "7000000001"
ANYTIME = {"start": None, "end": None, "kind": "anytime", "label": "anytime"}

# timing -> (days after local today, the window as the modal sends it, the window as every order
# payload publishes it). At the frozen local noon a future day is before its release instant, so a
# row lands `rescheduled`; today is past the 08:00 roster start, so it lands `scheduled` (R5) --
# unless the order is still PENDING, whose row is held on either date (R23's corollary).
TARGETS = {
    "future": (2, ("14:00", "18:00"), {"start": "14:00", "end": "18:00", "kind": "between", "label": "14:00-18:00"}),
    "due_today": (0, ("19:00", None), {"start": "19:00", "end": None, "kind": "after", "label": "after 19:00"}),
}

# Every state R2 lets an admin move. None is "no Delivery row yet".
PRE_STATUSES = [None, S.SCHEDULED, S.PENDING, S.ASSIGNED, S.PICKED_UP, S.IN_TRANSIT, S.ARRIVED, S.FAILED, S.RESCHEDULED]
MATRIX = [(pre, timing) for pre in PRE_STATUSES for timing in TARGETS]
MATRIX_IDS = [f"{pre.value if pre else 'no-row'}-{timing}" for pre, timing in MATRIX]

# Rows a driver holds carry `delivery_person_id`. FAILED keeps its driver: the failed tap never
# clears it.
DRIVER_HELD = frozenset({S.ASSIGNED, S.PICKED_UP, S.IN_TRANSIT, S.ARRIVED, S.FAILED, S.DELIVERED, S.RETURNED})
# ...of which these still sit on the driver's route today...
ON_ROUTE = frozenset({S.ASSIGNED, S.PICKED_UP, S.IN_TRANSIT, S.ARRIVED, S.FAILED})
# ...and these have bottles on the truck: bound to the driver's open session and counted in
# `current_active_deliveries`. A failed tap has already unbound its order.
ON_THE_TRUCK = frozenset({S.ASSIGNED, S.PICKED_UP, S.IN_TRANSIT, S.ARRIVED})

# The order status production pairs with each delivery status: the picked-up sync writes
# OUT_FOR_DELIVERY directly, and a failed delivery keeps it (dev's only failed order is
# out_for_delivery).
ORDER_STATUS_FOR = {
    None: OrderStatus.CONFIRMED,
    S.SCHEDULED: OrderStatus.CONFIRMED,
    S.PENDING: OrderStatus.CONFIRMED,
    S.RESCHEDULED: OrderStatus.CONFIRMED,
    S.ASSIGNED: OrderStatus.CONFIRMED,
    S.PICKED_UP: OrderStatus.OUT_FOR_DELIVERY,
    S.IN_TRANSIT: OrderStatus.OUT_FOR_DELIVERY,
    S.ARRIVED: OrderStatus.OUT_FOR_DELIVERY,
    S.FAILED: OrderStatus.OUT_FOR_DELIVERY,
}

# Where each seeded row came from, for its one history row. SCHEDULED is the bounced shape --
# claimed, then returned to the pool -- that the old delete branch had to refuse.
PREVIOUS_STATUS = {
    S.SCHEDULED: S.ASSIGNED,
    S.PENDING: S.SCHEDULED,
    S.RESCHEDULED: S.SCHEDULED,
    S.ASSIGNED: S.SCHEDULED,
    S.PICKED_UP: S.ASSIGNED,
    S.IN_TRANSIT: S.PICKED_UP,
    S.ARRIVED: S.IN_TRANSIT,
    S.FAILED: S.ARRIVED,
    S.DELIVERED: S.ARRIVED,
    S.CANCELLED: S.SCHEDULED,
    S.RETURNED: S.FAILED,
}


class Seeded(NamedTuple):
    order_id: int
    order_number: str
    delivery_id: Optional[int]
    route_id: Optional[int]
    original_date: date


@pytest.fixture
def local_noon():
    """Noon in Tashkent on the real local date, on every clock the reschedule path reads.

    The real DATE, frozen only to noon: `RouteOptimizationService.current_route` finds today's
    route row against the real wall clock, so an invented date would lose the route these tests
    check. The container runs in UTC and the app in Tashkent, hence three patches (spec §7).
    """
    now_local = datetime.combine(datetime.now(TZ).date(), time(12, 0), tzinfo=TZ)
    with patch("business_app.utils.delivery_window.local_now", return_value=now_local), patch(
        "business_app.utils.local_windows.local_now", return_value=now_local
    ), patch(
        "business_app.services.order_schedule_service.get_utc_now",
        return_value=now_local.astimezone(timezone.utc),
    ):
        yield now_local


@pytest.fixture
def rostered_driver(db):
    """The only rostered driver, so the release instant is 08:00 local on any date.

    `working_hours_start` is explicit: a changed column default would silently move which side
    of release a due-today cell lands on.
    """
    user = User(
        email="resched-driver@example.com",
        phone="+998901239001",
        password_hash=hash_password("DriverPassword123!"),
        first_name="Rustam",
        last_name="Haydovchi",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
        telegram_id=DRIVER_TELEGRAM_ID,
    )
    db.session.add(user)
    db.session.flush()
    db.session.add(
        DeliveryPerson(
            user_id=user.id,
            full_name="Rustam Haydovchi",
            phone=user.phone,
            working_hours_start="08:00",
            working_hours_end="20:00",
            is_active=True,
            is_available=True,
        )
    )
    db.session.commit()
    return user


@pytest.fixture
def manager_headers(db):
    """A MANAGER: `validate_admin_action` reads the role off the DB row and grants `manage_orders`."""
    manager = User(
        email="resched-manager@example.com",
        phone="+998901239002",
        password_hash=hash_password("ManagerPassword123!"),
        first_name="Malika",
        last_name="Menejer",
        user_type=UserType.STAFF,
        role=UserRole.MANAGER,
        is_verified=True,
    )
    db.session.add(manager)
    db.session.commit()
    token = create_access_token(identity=str(manager.id), additional_claims={"role": "manager"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture
def audit_events():
    """Every `audit_logger.log_event` call, bound against the real method's signature.

    A call the real method would reject raises TypeError here too. The recorder only keeps the row
    out of the database, so each call's arguments can be compared whole.
    """
    signature = inspect.signature(AuditLogger.log_event)
    events = []

    def _record(*args, **kwargs):
        bound = signature.bind(audit_logger, *args, **kwargs)
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        del arguments["self"]
        events.append(arguments)
        return "recorded-audit-event"

    with patch.object(audit_logger, "log_event", new=_record):
        yield events


def _rescheduled_events(events):
    return [event for event in events if event["action"] == "order_rescheduled"]


def _seed(address, driver, delivery_status, today, *, order_status=None) -> Seeded:
    """One order in the shape production leaves it, with everything a driver's hold attaches.

    Rows awaiting release (no row yet, or a held `rescheduled` one) are dated tomorrow; every
    other row is today's delivery.
    """
    original_date = today + timedelta(days=1) if delivery_status in (None, S.RESCHEDULED) else today
    order = Order(
        user_id=address.user_id,
        status=order_status or ORDER_STATUS_FOR[delivery_status],
        total_amount=50000,
        payment_method=PaymentMethod.CASH,
        delivery_date=original_date,
        order_source="admin",
        delivery_address_id=address.id,
    )
    db.session.add(order)
    db.session.flush()
    order_id, order_number = order.id, order.order_number
    if delivery_status is None:
        db.session.commit()
        return Seeded(order_id, order_number, None, None, original_date)

    held_by = driver.id if delivery_status in DRIVER_HELD else None
    delivery = Delivery(
        order_id=order_id,
        status=delivery_status,
        delivery_person_id=held_by,
        scheduled_date=datetime.combine(original_date, time.min, tzinfo=timezone.utc),
        scheduled_time_slot="anytime",
        distance_km=3.2,
        # A held row carries no ETA (spec §3.3); every other row a stale one to be replaced.
        estimated_delivery_time=(
            None if delivery_status == S.RESCHEDULED else datetime.now(timezone.utc) + timedelta(hours=1)
        ),
        delivery_attempts=1 if delivery_status == S.FAILED else 0,
        failed_delivery_reason="customer_absent" if delivery_status == S.FAILED else None,
    )
    db.session.add(delivery)
    db.session.flush()
    delivery_id = delivery.id
    db.session.add(
        DeliveryStatusHistory(
            delivery_id=delivery_id,
            old_status=PREVIOUS_STATUS[delivery_status],
            new_status=delivery_status,
            changed_by=held_by,
            changed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
    )
    if delivery_status in ON_THE_TRUCK:
        session = DriverBottleSession(
            driver_user_id=driver.id, bottles_loaded=20, status=DriverBottleSessionStatus.OPEN
        )
        db.session.add(session)
        db.session.flush()
        db.session.add(
            DriverBottleSessionOrder(session_id=session.id, order_id=order_id, accepted_by_driver_id=driver.id)
        )
        DeliveryPerson.query.filter_by(user_id=driver.id).one().current_active_deliveries = 1
    route_id = None
    if delivery_status in ON_ROUTE:
        route = DeliveryRoute(
            name="Bugungi marshrut",
            delivery_person_id=driver.id,
            start_location_lat=41.30,
            start_location_lng=69.24,
            route_date=datetime.now(timezone.utc),
            optimized_order=[delivery_id],
        )
        db.session.add(route)
        db.session.flush()
        route_id = route.id
    db.session.commit()
    return Seeded(order_id, order_number, delivery_id, route_id, original_date)


def _reschedule(client, headers, order_id, target, window=(None, None), *, reason=REASON):
    """The body the Reschedule modal sends: the `delivery_date` key is always present (spec §6)."""
    body = {
        "delivery_date": target.isoformat() if target is not None else None,
        "delivery_window_start": window[0],
        "delivery_window_end": window[1],
    }
    if reason is not None:
        body["reason"] = reason
    return client.patch(SCHEDULE_URL.format(order_id), headers=headers, json=body)


def _error_code(resp):
    return resp.get_json()["data"]["error_code"]


def _history(delivery_id):
    return DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).order_by(DeliveryStatusHistory.id).all()


# ---------------------------------------------------------------------------
# The matrix (spec §7): every reschedulable state x (a future day, today).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pre_status,timing", MATRIX, ids=MATRIX_IDS)
def test_a_reschedule_lands_every_state_where_the_spec_says(
    client,
    db,
    admin_claim_headers,
    admin_user,
    user_address,
    rostered_driver,
    local_noon,
    audit_events,
    pre_status,
    timing,
):
    today = local_noon.date()
    seed = _seed(user_address, rostered_driver, pre_status, today)
    days, window, published_window = TARGETS[timing]
    target = today + timedelta(days=days)
    # R5 (every matrix order is confirmed or out for delivery; R23's pending cells are below). A cell
    # with no row commits no delivery status of its own.
    landed = None if pre_status is None else (S.SCHEDULED if timing == "due_today" else S.RESCHEDULED)

    resp = _reschedule(client, admin_claim_headers, seed.order_id, target, window)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()["data"]["order"]
    assert body["delivery_date"] == target.isoformat()
    assert body["delivery_window"] == published_window
    assert body["status"] == "confirmed"
    # The detail metadata rides on the answer, so the modal re-renders from it (spec §5).
    assert body["can_reschedule"] is True
    assert body["reschedule_block_code"] is None
    assert body["reschedule_min_date"] == today.isoformat()
    assert body["reschedule_driver_losing_stop"] is None

    db.session.expire_all()
    order = db.session.get(Order, seed.order_id)
    assert order.delivery_date == target
    assert (order.delivery_window_start, order.delivery_window_end) == tuple(
        time.fromisoformat(value) if value else None for value in window
    )
    # R6: out_for_delivery goes back to confirmed, with a history row that carries no free text --
    # the customer bot prints OrderStatusHistory notes on its Track screen. It is the only order
    # move a reschedule makes (R23; the pending-order cells below pin the other side).
    assert order.status == OrderStatus.CONFIRMED
    order_history = [
        (row.old_status, row.new_status, row.changed_by, row.notes)
        for row in OrderStatusHistory.query.filter_by(order_id=order.id).order_by(OrderStatusHistory.id)
    ]
    if ORDER_STATUS_FOR[pre_status] == OrderStatus.OUT_FOR_DELIVERY:
        assert order_history == [(OrderStatus.OUT_FOR_DELIVERY, OrderStatus.CONFIRMED, admin_user.id, None)]
    else:
        assert order_history == []

    delivery = Delivery.query.filter_by(order_id=order.id).one_or_none()
    if pre_status is None and timing == "future":
        assert delivery is None  # still awaiting release: nothing for a driver to see
    else:
        # A due no-row order is released by the gate straight after the commit.
        assert delivery.status == (landed or S.SCHEDULED)
        assert delivery.delivery_person_id is None  # R4
        assert delivery.scheduled_date.date() == target
        assert delivery.scheduled_time_slot == published_window["label"]
        if delivery.status == S.RESCHEDULED:
            assert delivery.estimated_delivery_time is None  # no hours countdown for a held row
        else:
            assert delivery.estimated_delivery_time is not None
        # R8: the failure reason goes, the attempt count stays.
        assert delivery.failed_delivery_reason is None
        assert delivery.delivery_attempts == (1 if pre_status == S.FAILED else 0)

    if seed.delivery_id is not None:
        history = _history(seed.delivery_id)
        if landed != pre_status:
            assert len(history) == 2
            newest = history[-1]
            assert (newest.old_status, newest.new_status, newest.changed_by, newest.reason, newest.notes) == (
                pre_status,
                landed,
                admin_user.id,
                REASON,
                f"Rescheduled to {target.isoformat()}",
            )
        else:
            assert len(history) == 1  # the status did not change, so no history row (R9)

    # R7: bottles on the truck are only unbound; the stop leaves today's route; counters resync.
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).count() == 0
    if seed.route_id is not None:
        assert db.session.get(DeliveryRoute, seed.route_id).optimized_order == []
    assert DeliveryPerson.query.filter_by(user_id=rostered_driver.id).one().current_active_deliveries == 0

    events = _rescheduled_events(audit_events)
    assert events == [
        {
            "event_type": AuditEventType.ORDER_UPDATED,
            "action": "order_rescheduled",
            "severity": AuditSeverity.HIGH,
            "resource_type": "order",
            "resource_id": str(order.id),
            "description": None,
            "old_values": None,
            "new_values": None,
            "success": True,
            "error_message": None,
            "duration_ms": None,
            "additional_data": {
                "actor_user_id": admin_user.id,
                "from_delivery_date": seed.original_date.isoformat(),
                "from_window": ANYTIME,
                "to_delivery_date": target.isoformat(),
                "to_window": published_window,
                "delivery_status_before": pre_status.value if pre_status else None,
                "driver_before": rostered_driver.id if pre_status in DRIVER_HELD else None,
                "landed_status": landed.value if landed else None,
                "reason": REASON,
            },
        }
    ]
    # The audit row's JSON column has to hold it: a `date` in there is dropped with only a log line.
    additional = events[0]["additional_data"]
    assert json.loads(json.dumps(additional)) == additional


# ---------------------------------------------------------------------------
# R23: a reschedule never confirms an order.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timing", list(TARGETS))
def test_a_reschedule_never_confirms_a_pending_order(
    client,
    db,
    admin_claim_headers,
    admin_user,
    user_address,
    rostered_driver,
    local_noon,
    audit_events,
    timing,
):
    """A PENDING (unpaid) order that already owns a pool row. Rows are created for confirmed orders
    only, so this is a legacy shape, but R1 lets an admin move it. Confirming it on the way would go
    through `update_order_status` and run the CONFIRMED side effects (inventory confirmation, the
    release gate) for an order nobody has paid for.

    R23's corollary: the row is held in `rescheduled` on BOTH dates, today included. The release gate
    never offers an unconfirmed order to drivers, so a `scheduled` landing would put an unpaid order
    in today's pool. Held, it waits exactly as an order with no row does, until confirming the order
    runs `ensure_delivery_if_due`."""
    today = local_noon.date()
    seed = _seed(user_address, rostered_driver, S.SCHEDULED, today, order_status=OrderStatus.PENDING)
    days, window, _published_window = TARGETS[timing]
    target = today + timedelta(days=days)

    resp = _reschedule(client, admin_claim_headers, seed.order_id, target, window)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["data"]["order"]["status"] == "pending"
    db.session.expire_all()
    order = db.session.get(Order, seed.order_id)
    assert (order.status, order.delivery_date) == (OrderStatus.PENDING, target)
    assert OrderStatusHistory.query.filter_by(order_id=seed.order_id).count() == 0
    delivery = db.session.get(Delivery, seed.delivery_id)
    assert (delivery.status, delivery.delivery_person_id) == (S.RESCHEDULED, None)
    assert delivery.scheduled_date.date() == target
    assert delivery.estimated_delivery_time is None  # held: no hours countdown, on either date
    # The seeded row, then the reschedule's own. SCHEDULED -> RESCHEDULED in both cells: the due
    # date does not put an unconfirmed order back in the pool.
    assert [
        (row.old_status, row.new_status, row.changed_by, row.reason, row.notes) for row in _history(seed.delivery_id)
    ] == [
        (S.ASSIGNED, S.SCHEDULED, None, None, None),
        (S.SCHEDULED, S.RESCHEDULED, admin_user.id, REASON, f"Rescheduled to {target.isoformat()}"),
    ]
    additional = _rescheduled_events(audit_events)[0]["additional_data"]
    assert (additional["delivery_status_before"], additional["landed_status"]) == ("scheduled", "rescheduled")


# ---------------------------------------------------------------------------
# Refusals (R1, R2, R10, R11) -- each leaves the order and its delivery exactly as they were.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "order_status,delivery_status,error_code",
    [
        (OrderStatus.CANCELLED, None, "ORDER_NOT_RESCHEDULABLE"),
        (OrderStatus.CANCELLED, S.CANCELLED, "ORDER_NOT_RESCHEDULABLE"),
        (OrderStatus.DELIVERED, S.DELIVERED, "ORDER_NOT_RESCHEDULABLE"),
        (OrderStatus.RETURNED, S.RETURNED, "ORDER_NOT_RESCHEDULABLE"),
        # A live order whose delivery is finished. The first is the driver-bot cancel orphan (dev
        # order 153): refused, not repaired.
        (OrderStatus.CONFIRMED, S.CANCELLED, "DELIVERY_NOT_RESCHEDULABLE"),
        (OrderStatus.OUT_FOR_DELIVERY, S.DELIVERED, "DELIVERY_NOT_RESCHEDULABLE"),
        (OrderStatus.OUT_FOR_DELIVERY, S.RETURNED, "DELIVERY_NOT_RESCHEDULABLE"),
    ],
    ids=[
        "cancelled-order-no-row",
        "cancelled-order",
        "delivered-order",
        "returned-order",
        "live-order-cancelled-delivery",
        "live-order-delivered-delivery",
        "live-order-returned-delivery",
    ],
)
def test_a_finished_order_or_delivery_is_refused_and_left_as_it_was(
    client,
    db,
    admin_claim_headers,
    user_address,
    rostered_driver,
    local_noon,
    audit_events,
    order_status,
    delivery_status,
    error_code,
):
    """The old endpoint answered 200 for a cancelled order and would have deleted a delivered
    order's stale pool row. Neither may move now."""
    today = local_noon.date()
    seed = _seed(user_address, rostered_driver, delivery_status, today, order_status=order_status)

    resp = _reschedule(client, admin_claim_headers, seed.order_id, today + timedelta(days=2), ("14:00", "18:00"))

    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert _error_code(resp) == error_code
    db.session.expire_all()
    order = db.session.get(Order, seed.order_id)
    assert (order.status, order.delivery_date, order.delivery_window_start) == (order_status, seed.original_date, None)
    delivery = Delivery.query.filter_by(order_id=seed.order_id).one_or_none()
    assert (delivery.status if delivery else None) == delivery_status
    assert _rescheduled_events(audit_events) == []


def test_a_released_order_cannot_have_its_date_cleared(
    client, db, admin_claim_headers, user_address, rostered_driver, local_noon, audit_events
):
    """R10: `null` means "no schedule, release now", which only makes sense before release. Once a
    row exists there is nothing left to release, and a held row with no date would never come back."""
    today = local_noon.date()
    seed = _seed(user_address, rostered_driver, S.IN_TRANSIT, today)

    resp = _reschedule(client, admin_claim_headers, seed.order_id, None)

    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert _error_code(resp) == "DELIVERY_DATE_REQUIRED"
    db.session.expire_all()
    delivery = db.session.get(Delivery, seed.delivery_id)
    assert (delivery.status, delivery.delivery_person_id) == (S.IN_TRANSIT, rostered_driver.id)
    assert db.session.get(Order, seed.order_id).delivery_date == today
    assert _rescheduled_events(audit_events) == []


def test_a_date_after_the_grocery_contract_ends_is_refused(
    client, db, admin_claim_headers, user_address, rostered_driver, local_noon, audit_events
):
    """R11: a grocery store's AMOUNT contract ends in three days. A delivery after that would be
    charged against a contract that no longer exists; the contract's last day is still fine, and
    it is exactly the `reschedule_max_date` the modal was given."""
    today = local_noon.date()
    store = db.session.get(User, user_address.user_id)
    store.user_type = UserType.ENTITY
    store.entity_subtype = EntitySubtype.GROCERY_STORE
    contract = CorporateContract(
        user_id=store.id,
        contract_number=f"CTR-{uuid4().hex[:10]}",
        name="Bahor savdo",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(timezone.utc) - timedelta(days=30),
        end_date=datetime.combine(today + timedelta(days=3), time(12, 0), tzinfo=TZ).astimezone(timezone.utc),
        currency="UZS",
        is_active=True,
    )
    contract.tracking_mode = CorporateContractTrackingMode.AMOUNT
    db.session.add(contract)
    db.session.commit()
    seed = _seed(user_address, rostered_driver, S.SCHEDULED, today)

    past_end = _reschedule(client, admin_claim_headers, seed.order_id, today + timedelta(days=5), ("14:00", "18:00"))

    assert past_end.status_code == 400, past_end.get_data(as_text=True)
    assert _error_code(past_end) == "ORDER_RESCHEDULE_PAST_CONTRACT_END"
    db.session.expire_all()
    assert db.session.get(Order, seed.order_id).delivery_date == today
    assert db.session.get(Delivery, seed.delivery_id).status == S.SCHEDULED
    assert _rescheduled_events(audit_events) == []

    last_day = _reschedule(client, admin_claim_headers, seed.order_id, today + timedelta(days=3), ("14:00", "18:00"))

    assert last_day.status_code == 200, last_day.get_data(as_text=True)
    assert last_day.get_json()["data"]["order"]["reschedule_max_date"] == (today + timedelta(days=3)).isoformat()
    db.session.expire_all()
    assert db.session.get(Delivery, seed.delivery_id).status == S.RESCHEDULED


def test_a_reason_the_history_row_cannot_hold_is_refused_before_anything_moves(
    client, db, admin_claim_headers, user_address, rostered_driver, local_noon, audit_events
):
    """R24 (owner, 2026-09-24). R9 keeps the reason on the history row, whose column is String(100).
    Postgres would raise on a longer one after the locks were taken, a 500. So the service strips it
    and refuses anything longer up front, with a code the modal maps. It is never truncated: the
    audit would keep the whole text and the history only a prefix of it."""
    from business_app.services.order_schedule_service import RESCHEDULE_REASON_MAX_LENGTH

    assert RESCHEDULE_REASON_MAX_LENGTH == 100
    today = local_noon.date()
    seed = _seed(user_address, rostered_driver, S.ASSIGNED, today)
    target = today + timedelta(days=2)

    not_text = _reschedule(client, admin_claim_headers, seed.order_id, target, ("14:00", "18:00"), reason=42)
    assert not_text.status_code == 400, not_text.get_data(as_text=True)
    assert "reason must be a string" in not_text.get_data(as_text=True)

    too_long = _reschedule(client, admin_claim_headers, seed.order_id, target, ("14:00", "18:00"), reason="x" * 101)
    assert too_long.status_code == 400, too_long.get_data(as_text=True)
    assert _error_code(too_long) == "ORDER_RESCHEDULE_REASON_TOO_LONG"
    db.session.expire_all()
    delivery = db.session.get(Delivery, seed.delivery_id)
    assert (delivery.status, delivery.delivery_person_id) == (S.ASSIGNED, rostered_driver.id)
    assert _rescheduled_events(audit_events) == []

    at_limit = _reschedule(
        client, admin_claim_headers, seed.order_id, target, ("14:00", "18:00"), reason="  " + "y" * 100 + "  "
    )
    assert at_limit.status_code == 200, at_limit.get_data(as_text=True)
    assert _history(seed.delivery_id)[-1].reason == "y" * 100  # stripped, then stored whole


@pytest.mark.parametrize(
    "headers_fixture,expected_status",
    [("manager_headers", 200), ("operator_auth_headers", 403), ("driver_auth_headers", 403)],
    ids=["manager", "operator", "driver"],
)
def test_admins_and_managers_may_reschedule_operators_and_drivers_may_not(
    request,
    client,
    db,
    user_address,
    rostered_driver,
    local_noon,
    audit_events,
    headers_fixture,
    expected_status,
):
    """R17: the endpoint's existing `manage_orders`/`edit_orders` gate, read off the user row."""
    headers = request.getfixturevalue(headers_fixture)
    today = local_noon.date()
    seed = _seed(user_address, rostered_driver, S.ASSIGNED, today)

    resp = _reschedule(client, headers, seed.order_id, today + timedelta(days=2), ("14:00", "18:00"))

    assert resp.status_code == expected_status, resp.get_data(as_text=True)
    db.session.expire_all()
    delivery = db.session.get(Delivery, seed.delivery_id)
    if expected_status == 200:
        assert (delivery.status, delivery.delivery_person_id) == (S.RESCHEDULED, None)
        assert len(_rescheduled_events(audit_events)) == 1
    else:
        assert (delivery.status, delivery.delivery_person_id) == (S.ASSIGNED, rostered_driver.id)
        assert db.session.get(Order, seed.order_id).delivery_date == today
        assert _rescheduled_events(audit_events) == []


# ---------------------------------------------------------------------------
# Locking (spec §3.4 step 1): the decision is made on the rows as they are under the locks.
# ---------------------------------------------------------------------------


def test_the_reschedule_decides_on_the_row_it_locked_not_a_stale_copy(
    client, db, admin_claim_headers, admin_user, user_address, rostered_driver, local_noon, audit_events
):
    """A driver's tap committed after this session last read the delivery, so the identity map
    still holds the old driverless `scheduled` copy. `with_for_update()` alone would hand that copy
    back (see `ensure_delivery_if_due`). The driver would then never be unassigned, because
    clearing an attribute that already reads None is not a change to flush, and the row would rest
    in the pool with a driver on it."""
    today = local_noon.date()
    seed = _seed(user_address, rostered_driver, S.SCHEDULED, today)
    stale = db.session.get(Delivery, seed.delivery_id)
    assert (stale.status, stale.delivery_person_id) == (S.SCHEDULED, None)
    db.session.execute(
        text("UPDATE deliveries SET status = 'in_transit', delivery_person_id = :driver WHERE id = :delivery"),
        {"driver": rostered_driver.id, "delivery": seed.delivery_id},
    )
    assert stale.status == S.SCHEDULED  # the identity map did not hear about it

    resp = _reschedule(client, admin_claim_headers, seed.order_id, today + timedelta(days=2), ("14:00", "18:00"))

    assert resp.status_code == 200, resp.get_data(as_text=True)
    db.session.expire_all()
    delivery = db.session.get(Delivery, seed.delivery_id)
    assert (delivery.status, delivery.delivery_person_id) == (S.RESCHEDULED, None)
    newest = _history(seed.delivery_id)[-1]
    assert (newest.old_status, newest.new_status, newest.changed_by) == (S.IN_TRANSIT, S.RESCHEDULED, admin_user.id)
    additional = _rescheduled_events(audit_events)[0]["additional_data"]
    assert (additional["delivery_status_before"], additional["driver_before"]) == ("in_transit", rostered_driver.id)


def test_a_row_released_between_the_two_locks_is_pulled_back_not_skipped(
    client, db, admin_claim_headers, user_address, rostered_driver, local_noon, audit_events, monkeypatch
):
    """The Delivery is locked before the Order, so a release that commits between the two leaves
    the first read empty. Simulated by making the first read miss a row that is there. The re-read
    under the Order lock has to find it and take the driver off. Treating the order as never
    released would move only its date and leave the stop on the driver's truck."""
    today = local_noon.date()
    seed = _seed(user_address, rostered_driver, S.IN_TRANSIT, today)
    real_lock = OrderScheduleService._lock_delivery
    reads = []

    def _first_read_misses(order_id):
        reads.append(order_id)
        return None if len(reads) == 1 else real_lock(order_id)

    monkeypatch.setattr(OrderScheduleService, "_lock_delivery", staticmethod(_first_read_misses))

    resp = _reschedule(client, admin_claim_headers, seed.order_id, today + timedelta(days=2), ("14:00", "18:00"))

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert reads == [seed.order_id, seed.order_id]
    db.session.expire_all()
    delivery = db.session.get(Delivery, seed.delivery_id)
    assert (delivery.status, delivery.delivery_person_id) == (S.RESCHEDULED, None)
    assert _history(seed.delivery_id)[-1].old_status == S.IN_TRANSIT
    assert DriverBottleSessionOrder.query.filter_by(order_id=seed.order_id).count() == 0


# ---------------------------------------------------------------------------
# Request-shape validation owned by the endpoint.
# ---------------------------------------------------------------------------


def _headers(app, admin_user):
    with app.app_context():
        token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}"}


def test_reschedule_without_delivery_date_key_is_rejected(
    client, db, admin_claim_headers, user_address, rostered_driver, local_noon
):
    """`data.get('delivery_date')` returns None for both 'key missing' and
    'key present as null' -- only the latter should mean 'clear the
    schedule'. A PATCH that forgets to send delivery_date (e.g. a UI update
    that only touches the window) must be rejected outright, not silently
    clear the date and make the order immediately due.
    """
    today = local_noon.date()
    seed = _seed(user_address, rostered_driver, S.SCHEDULED, today)

    resp = client.patch(
        SCHEDULE_URL.format(seed.order_id), headers=admin_claim_headers, json={"delivery_window_start": "19:00"}
    )

    assert resp.status_code == 400
    assert "delivery_date" in resp.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Delivery, seed.delivery_id).status == S.SCHEDULED
    assert db.session.get(Order, seed.order_id).delivery_date == today


def test_reschedule_rejects_an_unparseable_window(
    client, db, admin_claim_headers, user_address, rostered_driver, local_noon
):
    """Malformed input must surface as the shared validator's 400, not a 500 --
    and must not touch the order or its delivery."""
    today = local_noon.date()
    seed = _seed(user_address, rostered_driver, S.SCHEDULED, today)

    resp = _reschedule(client, admin_claim_headers, seed.order_id, today + timedelta(days=3), (None, "25:99"))

    assert resp.status_code == 400
    assert "Invalid delivery schedule" in resp.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Delivery, seed.delivery_id).status == S.SCHEDULED
    assert db.session.get(Order, seed.order_id).delivery_date == today


def test_reschedule_a_nonexistent_order_is_404(app, client, admin_user):
    resp = client.patch(
        SCHEDULE_URL.format(999999999),
        headers=_headers(app, admin_user),
        json={"delivery_date": (date.today() + timedelta(days=3)).isoformat()},
    )
    assert resp.status_code == 404


def test_reschedule_with_explicit_null_clears_the_schedule(app, client, admin_user, user_address):
    """The other half of the absent-vs-null distinction: explicit `null` is a
    legitimate request to clear the schedule, and must still work -- clearing
    means 'no schedule', which is today's immediate-release behaviour, so the
    order gets a Delivery right away rather than sitting awaiting release.
    """
    with app.app_context():
        order = Order(
            user_id=user_address.user_id,
            status=OrderStatus.CONFIRMED,
            total_amount=50000,
            delivery_date=date.today() + timedelta(days=5),
            order_source="admin",
            delivery_address_id=user_address.id,
        )
        db.session.add(order)
        db.session.commit()
        order_id = order.id

    resp = client.patch(
        f"/api/v1/admin/orders/{order_id}/schedule",
        headers=_headers(app, admin_user),
        json={"delivery_date": None},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()["data"]["order"]
    assert body["delivery_date"] is None

    with app.app_context():
        order = Order.query.get(order_id)
        assert order.delivery_date is None
        assert Delivery.query.filter_by(order_id=order_id).first() is not None


# ---------------------------------------------------------------------------
# The release gate, driven through the endpoint. `reschedule` refuses a
# finished order (R1) but accepts a PENDING one, and leaves the question "may
# this order have a Delivery yet?" to `OrderScheduleService.ensure_delivery_
# if_due`, the ONE place that answers it.
# ---------------------------------------------------------------------------


def _pending_order(app, user_address, *, status=OrderStatus.PENDING):
    """Carries a real, in-polygon address so `DeliveryService.create_delivery`
    would genuinely SUCCEED if the gate let it through. Without the address it
    would raise, the endpoint's `except Exception` would turn that into a 500,
    and "no Delivery row" would be true for the wrong reason."""
    with app.app_context():
        order = Order(
            user_id=user_address.user_id,
            status=status,
            total_amount=50000,
            delivery_date=date.today(),
            order_source="web",
            delivery_address_id=user_address.id,
        )
        db.session.add(order)
        db.session.commit()
        return order.id


def test_rescheduling_a_pending_order_does_not_push_it_to_drivers(app, client, admin_user, user_address):
    """A still-PENDING (unpaid card) order re-dated to tomorrow must NOT get a
    Delivery row, and must not broadcast to a single driver.

    `is_awaiting_release` reports False here (a PENDING order is not being held
    back, it is simply not a release candidate), and the gate used to read that
    False as "due now" -- so this PATCH created the delivery, fired the
    diversion evaluator and put an Accept button for an unpaid order dated
    tomorrow in front of every on-shift driver.
    """
    from unittest.mock import patch as _patch

    order_id = _pending_order(app, user_address)
    target = (date.today() + timedelta(days=1)).isoformat()

    with _patch(
        "business_app.tasks.delivery_tasks.evaluate_pool_insertion_suggestions_task.delay"
    ) as evaluate, _patch("business_app.tasks.staff_tasks.notify_staff_new_order.delay") as broadcast:
        resp = client.patch(
            f"/api/v1/admin/orders/{order_id}/schedule",
            headers=_headers(app, admin_user),
            json={"delivery_date": target},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        evaluate.assert_not_called()
        broadcast.assert_not_called()

    with app.app_context():
        order = Order.query.get(order_id)
        assert order.delivery_date.isoformat() == target  # the re-date itself still applied
        assert Delivery.query.filter_by(order_id=order_id).first() is None


def test_clearing_the_schedule_of_a_confirmed_order_still_releases_it(app, client, admin_user, user_address):
    """The normal path must be untouched by the status precondition: a
    CONFIRMED order whose schedule is cleared is due right now and still gets
    its Delivery, broadcast included."""
    from unittest.mock import patch as _patch

    order_id = _pending_order(app, user_address, status=OrderStatus.CONFIRMED)

    with _patch("business_app.tasks.delivery_tasks.evaluate_pool_insertion_suggestions_task.delay") as evaluate:
        resp = client.patch(
            f"/api/v1/admin/orders/{order_id}/schedule",
            headers=_headers(app, admin_user),
            json={"delivery_date": None},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        evaluate.assert_called_once()

    with app.app_context():
        assert Delivery.query.filter_by(order_id=order_id).first() is not None


# ---------------------------------------------------------------------------
# Post-commit fan-out (spec §3.4 step 6, R13, R16). Who hears about a reschedule
# is written out here, not read from the service's own sets: a test that
# imported DRIVER_NOTICE_DELIVERY_STATUSES would pass whatever that set said.
# ---------------------------------------------------------------------------

# R13: the customer is told only when the delivery was already on its way.
CUSTOMER_TOLD = frozenset({S.IN_TRANSIT, S.ARRIVED})
# R16: the driver who loses a stop off their live route. Not a failed row, not a pool row.
DRIVER_TOLD = frozenset({S.ASSIGNED, S.PICKED_UP, S.IN_TRANSIT, S.ARRIVED})


class _FanOut:
    """What each of the four post-commit seams was asked to do, in call order."""

    SEAMS = ("offer", "customer", "driver", "route")

    def __init__(self):
        self.calls = {seam: [] for seam in self.SEAMS}
        self.failing = set()

    def record(self, seam, call):
        self.calls[seam].append(call)
        if seam in self.failing:
            raise RuntimeError(f"simulated {seam} outage")


@pytest.fixture
def fanout():
    """The reschedule's post-commit seams, each recorded against the real signature it stands in for.

    The Celery `.delay`s are replaced, because the broker is external I/O. `offer_to_drivers` is
    spied on and then runs for real, so the pool fan-out behind it is still exercised. The
    route-updated push is replaced at the staff-bot webhook itself (`notify_route_updated`, under
    the name `route_edit_service` imported it by), so `RouteEditService._notify_route_updated`,
    the wrapper the reschedule calls, runs for real: a changed argument or a swallowed error in it
    shows up here. Each call is recorded as its bound arguments with the defaults applied, so an
    omitted keyword and an explicit default read the same. A call with the wrong shape raises
    TypeError at the seam, and the recorder never sees it.
    """
    from business_app.services.delivery_service import DeliveryService
    from business_app.tasks.notification_tasks import send_delivery_rescheduled_notification_task
    from business_app.tasks.staff_tasks import notify_staff_order_unassigned
    from business_app.utils import bot_webhook

    recorder = _FanOut()
    real_offer = DeliveryService.offer_to_drivers
    customer_signature = inspect.signature(send_delivery_rescheduled_notification_task.run)
    driver_signature = inspect.signature(notify_staff_order_unassigned.run)
    route_signature = inspect.signature(bot_webhook.notify_route_updated)

    def _bound(signature, args, kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)

    def _offer(self, delivery):
        recorder.record("offer", delivery.id)
        return real_offer(self, delivery)

    def _customer(*args, **kwargs):
        recorder.record("customer", _bound(customer_signature, args, kwargs))

    def _driver(*args, **kwargs):
        recorder.record("driver", _bound(driver_signature, args, kwargs))

    def _route_push(*args, **kwargs):
        recorder.record("route", _bound(route_signature, args, kwargs))
        return True

    with patch.object(DeliveryService, "offer_to_drivers", new=_offer), patch.object(
        send_delivery_rescheduled_notification_task, "delay", new=_customer
    ), patch.object(notify_staff_order_unassigned, "delay", new=_driver), patch(
        "business_app.services.route_edit_service.notify_route_updated", new=_route_push
    ):
        yield recorder


@pytest.mark.parametrize("pre_status,timing", MATRIX, ids=MATRIX_IDS)
def test_each_reschedule_tells_exactly_the_people_it_affects(
    client,
    db,
    admin_claim_headers,
    user_address,
    rostered_driver,
    local_noon,
    fanout,
    pre_status,
    timing,
):
    today = local_noon.date()
    seed = _seed(user_address, rostered_driver, pre_status, today)
    days, window, _published_window = TARGETS[timing]
    target = today + timedelta(days=days)

    resp = _reschedule(client, admin_claim_headers, seed.order_id, target, window)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    delivery = Delivery.query.filter_by(order_id=seed.order_id).one_or_none()
    # Offered once when the row is newly in today's pool: a row already there is not a new order
    # to anyone, and a no-row order the gate releases is offered by `create_delivery` itself --
    # never a second time by the reschedule.
    newly_in_pool = timing == "due_today" and pre_status != S.SCHEDULED
    driver_loses_stop = pre_status in DRIVER_TOLD
    assert fanout.calls == {
        "offer": [delivery.id] if newly_in_pool else [],
        # Only the order id travels: the task re-reads the live order when it sends.
        "customer": [{"order_id": seed.order_id}] if pre_status in CUSTOMER_TOLD else [],
        # The card the driver last saw -- the order status as it was before the pull-back --
        # plus the new date, which switches the staff bot to the "rescheduled" copy.
        "driver": (
            [
                {
                    "telegram_id": DRIVER_TELEGRAM_ID,
                    "order_info": {
                        "delivery_id": seed.delivery_id,
                        "order_id": seed.order_id,
                        "order_number": seed.order_number,
                        "status": ORDER_STATUS_FOR[pre_status].value,
                        "total_amount": 50000.0,
                        "payment_method": "cash",
                        "delivery_address": user_address.full_address,
                        "rescheduled_to": target.isoformat(),
                    },
                }
            ]
            if driver_loses_stop
            else []
        ),
        # The existing route-updated card refresh, as the webhook receives it: the driver's users.id,
        # the sounded default, and no materiality verdict, because nothing re-solved the route.
        "route": (
            [
                {
                    "driver_id": rostered_driver.id,
                    "sound": True,
                    "materiality": None,
                    "trigger": None,
                    "event_id": None,
                }
            ]
            if driver_loses_stop
            else []
        ),
    }


def test_a_customer_told_of_an_earlier_reschedule_is_told_of_the_next(
    client, db, admin_claim_headers, user_address, rostered_driver, local_noon, fanout
):
    """R13's second clause. The earlier notice has since been marked read, which flips its
    `delivery_status` to `read`. The follow-up still goes out: the lookup keys on `is_sent` and
    this order's id, so a customer who has read one notice keeps getting the updates."""
    today = local_noon.date()
    seed = _seed(user_address, rostered_driver, S.RESCHEDULED, today)
    db.session.add(
        Notification(
            user_id=user_address.user_id,
            notification_type=NotificationType.DELIVERY_RESCHEDULED.value,
            channel=NotificationChannel.TELEGRAM,
            title="Yetkazib berish ko'chirildi",
            message=f"#{seed.order_number} buyurtmangizni yetkazib berish ko'chirildi.",
            is_sent=True,
            delivery_status=NotificationStatus.READ,
            order_id=seed.order_id,
            delivery_id=seed.delivery_id,
        )
    )
    db.session.commit()

    resp = _reschedule(client, admin_claim_headers, seed.order_id, today + timedelta(days=3), ("14:00", "18:00"))

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert fanout.calls == {"offer": [], "customer": [{"order_id": seed.order_id}], "driver": [], "route": []}


@pytest.mark.parametrize("failing", _FanOut.SEAMS)
def test_one_failed_notice_does_not_stop_the_others(
    client,
    db,
    admin_claim_headers,
    user_address,
    rostered_driver,
    local_noon,
    audit_events,
    fanout,
    failing,
):
    """An in-transit delivery moved to today touches every seam at once. Whichever one is down, the
    reschedule still answers 200 and is committed, and every other seam and the audit still run.

    A webhook outage (`route`) is absorbed by `RouteEditService._notify_route_updated`'s own guard;
    a broker outage on any Celery seam is absorbed by that step's `_best_effort`."""
    fanout.failing = {failing}
    today = local_noon.date()
    seed = _seed(user_address, rostered_driver, S.IN_TRANSIT, today)

    resp = _reschedule(client, admin_claim_headers, seed.order_id, today, ("19:00", None))

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert {seam: len(calls) for seam, calls in fanout.calls.items()} == {seam: 1 for seam in _FanOut.SEAMS}
    assert len(_rescheduled_events(audit_events)) == 1
    db.session.expire_all()
    delivery = db.session.get(Delivery, seed.delivery_id)
    assert (delivery.status, delivery.delivery_person_id) == (S.SCHEDULED, None)


def test_a_refused_reschedule_tells_nobody(
    client, db, admin_claim_headers, user_address, rostered_driver, local_noon, fanout
):
    """An in-transit row would notify the customer, the driver and the route card. Refused (its
    date cannot be cleared once released), it notifies none of them."""
    seed = _seed(user_address, rostered_driver, S.IN_TRANSIT, local_noon.date())

    resp = _reschedule(client, admin_claim_headers, seed.order_id, None)

    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert fanout.calls == {seam: [] for seam in _FanOut.SEAMS}


def test_a_pending_orders_held_row_moved_to_today_is_offered_to_nobody(
    client, db, admin_claim_headers, user_address, rostered_driver, local_noon, fanout, monkeypatch
):
    """R23's corollary at the seams. The `rescheduled-due_today` matrix cell (a confirmed order) is
    offered to drivers. The same held row on a still-PENDING order stays held, because the release
    gate never offers an unconfirmed order. So nothing reaches the broker on the way to drivers: no
    auto-assign timer, no evaluator, no direct broadcast. Nobody is told either: no driver held
    the row (R16), and the customer was never told of an earlier reschedule (R13). Confirming the
    order releases the row later."""
    from business_app.tasks import delivery_tasks, staff_tasks
    from tests.unit.test_delivery_service_business_rules import _task_spy

    auto_assign = _task_spy(monkeypatch, delivery_tasks.auto_assign_delivery_task, "apply_async")
    evaluator = _task_spy(monkeypatch, delivery_tasks.evaluate_pool_insertion_suggestions_task, "delay")
    broadcast = _task_spy(monkeypatch, staff_tasks.notify_staff_new_order, "delay")
    today = local_noon.date()
    seed = _seed(user_address, rostered_driver, S.RESCHEDULED, today, order_status=OrderStatus.PENDING)
    days, window, _published_window = TARGETS["due_today"]

    resp = _reschedule(client, admin_claim_headers, seed.order_id, today + timedelta(days=days), window)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert fanout.calls == {seam: [] for seam in _FanOut.SEAMS}
    assert (auto_assign, evaluator, broadcast) == ([], [], [])
    db.session.expire_all()
    delivery = db.session.get(Delivery, seed.delivery_id)
    assert (delivery.status, delivery.delivery_person_id) == (S.RESCHEDULED, None)
    assert db.session.get(Order, seed.order_id).status == OrderStatus.PENDING
