"""Re-dispatch is a reschedule to today (R18), driven through both of its buttons.

The admin Delivery page (`POST /api/v1/admin/deliveries/<id>/redispatch`) and the
operator staff-bot flow (`POST /api/v1/staff/delivery/redispatch/<id>`) both reach
`StaffService.redispatch_failed_delivery`, which is now
`OrderScheduleService.reschedule(date=local today, window unchanged,
expected_delivery_status=FAILED)`. It shares one code path with the admin Reschedule, so
the two cannot disagree on the unassign, the history row, the route, the order's date or
who is told.

Where it lands follows R5. After today's release (today + the earliest rostered shift
start) it is `scheduled` and offered to drivers at once. Before it, the delivery is
`rescheduled` and held until the shift starts, and both responses say when (R25). The
business clock is frozen on the three seams that read it (spec §7). The rostered
driver's `working_hours_start` is set explicitly, because it IS the release instant.

Mocked: Celery publishes only, through a recorder that binds every call against the
task's own signature. The bot tests run the real `RedispatchHandler` against the real
route through `_Bridge` (tests/integration/test_staff_bot_place_full_e2e.py). Telegram is
a MagicMock, and i18n echoes keys and records what each call interpolated.

Spec: docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md (R5, R18, R25, §3.6).
"""

import asyncio
import inspect
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute, DeliveryStatusHistory
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.tasks.delivery_tasks import (
    auto_assign_delivery_task,
    evaluate_pool_insertion_suggestions_task,
)
from business_app.utils.exceptions import ValidationError
from business_app.utils.password_security import hash_password
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, UserRole, UserType
from staff_bot.handlers.operator import redispatch as redispatch_module
from staff_bot.handlers.operator.redispatch import RedispatchHandler
from tests.integration.test_staff_bot_place_full_e2e import _Bridge
from tests.unit.test_delivery_service_business_rules import _unshadow_task_publish
from tests.unit.test_staff_bot_place_surfaces import _make_update_context, _patch_handler

pytestmark = pytest.mark.integration

TZ = ZoneInfo(DISPLAY_TIMEZONE)
ADMIN_REDISPATCH = "/api/v1/admin/deliveries/{}/redispatch"
STAFF_REDISPATCH = "/api/v1/staff/delivery/redispatch/{}"
POOL = "/api/v1/staff/delivery/pool"
REASON = "Customer is home after 18:00"
CUSTOMER_WINDOW = (time(9, 0), time(11, 0))
# The only rostered shift start, so it is today's release instant.
SHIFT_START = time(8, 0)

# What made each refusal stale, and the code the operator's screen branches on.
REFUSALS = {
    # Cancelled from another screen after the Re-dispatch list was drawn. This used to
    # be 409 STAFF_ORDER_NOT_ACTIVE on the staff route and an uncaught 500 on the admin one.
    "order_cancelled_meanwhile": "ORDER_NOT_RESCHEDULABLE",
    # A colleague re-dispatched it first: it is back in the pool, no longer FAILED.
    "already_redispatched": "STAFF_DELIVERY_NOT_REDISPATCHABLE",
}


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


@pytest.fixture
def enqueued(monkeypatch):
    """Every Celery publish, as `(task name, args, kwargs)`.

    Each call is bound against the task's own `run` signature first, so a publish whose
    arguments drifted from the task raises here. The session-wide no-op in
    tests/conftest.py would accept it silently.
    """
    from celery.app.task import Task

    _unshadow_task_publish(monkeypatch)
    calls = []

    def _record(task, args, kwargs):
        inspect.signature(task.run).bind(*args, **kwargs)
        calls.append((task.name, tuple(args), dict(kwargs)))
        return Mock(id="mock-task-id")

    def _delay(task, *args, **kwargs):
        return _record(task, args, kwargs)

    def _apply_async(task, args=None, kwargs=None, **options):
        return _record(task, args or (), kwargs or {})

    monkeypatch.setattr(Task, "delay", _delay)
    monkeypatch.setattr(Task, "apply_async", _apply_async)
    return calls


@pytest.fixture
def http(app):
    """A fresh client for the bot bridge. The session-scoped `client` shares a cookie jar."""
    return app.test_client()


@pytest.fixture
def echo_i18n(monkeypatch):
    """Render every staff-bot string as its key, and record `(key, interpolated values)`.

    Without this, a missing seed row renders as a humanised key tail, and a copy assertion
    would pass or fail depending on whether the seed ran. The record is how a test sees
    the `{time}` the held copy was given.
    """
    from staff_bot.i18n import i18n

    calls = []

    def _echo(key, language=None, *args, **kwargs):
        calls.append((key, kwargs))
        return key

    monkeypatch.setattr(i18n, "get", _echo)
    return calls


@pytest.fixture
def rostered_driver(db):
    """The driver the delivery failed with, and the only rostered shift: 08:00."""
    user = User(
        email="redispatch-driver@example.com",
        phone="+998901230001",
        password_hash=hash_password("DriverPassword123!"),
        first_name="Rustam",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.flush()
    db.session.add(
        DeliveryPerson(
            user_id=user.id,
            full_name="Rustam Driver",
            phone=user.phone,
            working_hours_start=SHIFT_START.strftime("%H:%M"),
            working_hours_end="20:00",
            is_active=True,
            is_available=True,
        )
    )
    db.session.commit()
    return user


class _OperatorBridge(_Bridge):
    """`_Bridge` plus the operator's re-dispatch call. It also keeps what the backend answered.

    The path is spelled out rather than rebuilt from `staff_bot.config`, because it IS the
    contract with business_app (the same reason as test_staff_delivery_journey_dispatcher.py).
    """

    def __init__(self, http, token):
        super().__init__(http, token)
        self.answers = []

    def _send(self, method, path, payload=None):
        response = super()._send(method, path, payload)
        self.answers.append((method, path, response.status_code, response.error_code))
        return response

    async def redispatch_delivery(self, token, delivery_id):
        return self._request("POST", STAFF_REDISPATCH.format(delivery_id))


def _token(app, user):
    with app.app_context():
        return create_access_token(identity=str(user.id))


def _bearer(app, user):
    return {"Authorization": f"Bearer {_token(app, user)}", "Content-Type": "application/json"}


def _freeze_business_clock(monkeypatch, local_time):
    """Pin "now" to `local_time` today, Tashkent, on the three seams that read it (spec §7).

    The date comes from the real clock, so every unpatched `datetime.now` in the path still
    agrees on which day it is.
    """
    from business_app.services import order_schedule_service
    from business_app.utils import delivery_window, local_windows

    frozen = datetime.combine(delivery_window.local_now().date(), local_time, tzinfo=TZ)
    monkeypatch.setattr(delivery_window, "local_now", lambda: frozen)
    monkeypatch.setattr(local_windows, "local_now", lambda: frozen)
    monkeypatch.setattr(order_schedule_service, "get_utc_now", lambda: frozen.astimezone(timezone.utc))
    return frozen


def _published_release(today):
    """Today's 08:00 Tashkent shift start as both routes publish it: UTC, ISO 8601."""
    return datetime.combine(today, SHIFT_START, tzinfo=TZ).astimezone(timezone.utc).isoformat()


def _failed_delivery(db, customer, driver, *, today, number):
    """A delivery that failed at the door yesterday.

    The order is OUT_FOR_DELIVERY, because the picked-up sync writes that and a failed
    attempt leaves it there. The FAILED row still carries its driver, a failure reason, one
    attempt and yesterday's ETA. As in `TestReturningToThePoolClearsTheRoute`, a stale stop
    sits on that driver's stored route, and the re-dispatch must drop it.
    """
    yesterday = today - timedelta(days=1)
    address = UserAddress(
        user_id=customer.id,
        title="Home",
        full_address=f"{number} Chilonzor 9, Tashkent",
        street_address="Chilonzor 9",
        city="Tashkent",
        latitude=41.29,
        longitude=69.21,
    )
    db.session.add(address)
    db.session.flush()
    order = Order(
        user_id=customer.id,
        order_number=number,
        status=OrderStatus.OUT_FOR_DELIVERY,
        subtotal=Decimal("45000.00"),
        delivery_fee=Decimal("0.00"),
        total_amount=Decimal("45000.00"),
        payment_method=PaymentMethod.CASH,
        delivery_address_id=address.id,
        delivery_date=yesterday,
        delivery_window_start=CUSTOMER_WINDOW[0],
        delivery_window_end=CUSTOMER_WINDOW[1],
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver.id,
        status=DeliveryStatus.FAILED,
        failed_delivery_reason="customer_unavailable",
        delivery_attempts=1,
        distance_km=4.2,
        scheduled_date=datetime.combine(yesterday, time(9, 0), tzinfo=TZ),
        scheduled_time_slot="09:00-11:00",
        estimated_delivery_time=datetime.combine(yesterday, time(10, 0), tzinfo=TZ),
    )
    db.session.add(delivery)
    db.session.flush()
    route = DeliveryRoute(
        name="r",
        delivery_person_id=driver.id,
        start_location_lat=41.30,
        start_location_lng=69.24,
        route_date=datetime.now(timezone.utc),
        optimized_order=[delivery.id],
    )
    db.session.add(route)
    db.session.commit()
    return delivery, route


def _make_stale(db, delivery, case):
    if case == "order_cancelled_meanwhile":
        delivery.order.status = OrderStatus.CANCELLED
    else:
        delivery.status = DeliveryStatus.SCHEDULED
        delivery.delivery_person_id = None
        delivery.failed_delivery_reason = None
    db.session.commit()


def _snapshot(delivery):
    """Everything a refused re-dispatch must leave exactly as it was."""
    order = delivery.order
    return (
        delivery.status,
        delivery.delivery_person_id,
        order.status,
        order.delivery_date,
        order.delivery_window_start,
        order.delivery_window_end,
        DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id).count(),
    )


def _last_history(delivery_id):
    return (
        DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id)
        .order_by(DeliveryStatusHistory.id.desc())
        .first()
    )


def _pool_ids(client, app, driver):
    response = client.get(POOL, headers=_bearer(app, driver))
    assert response.status_code == 200, response.get_data(as_text=True)
    return [item["delivery_id"] for item in response.get_json()["data"]["items"]]


# --------------------------------------------------------------------------- #
# 1. Landing: after today's release vs before it
# --------------------------------------------------------------------------- #


def test_admin_redispatch_after_todays_release_puts_it_back_in_the_pool_dated_today(
    app, client, db, admin_claim_headers, admin_user, rostered_driver, sample_user, enqueued, monkeypatch
):
    frozen = _freeze_business_clock(monkeypatch, time(12, 0))  # after the 08:00 shift start
    today = frozen.date()
    delivery, route = _failed_delivery(db, sample_user, rostered_driver, today=today, number="ORD-RD-ADMIN")
    delivery_id, order_id = delivery.id, delivery.order_id

    response = client.post(ADMIN_REDISPATCH.format(delivery_id), json={"reason": REASON}, headers=admin_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    data = response.get_json()["data"]
    assert (data["delivery"]["status"], data["delivery"]["driver_id"]) == ("scheduled", None)
    # Drivers can see it now, so there is no "when" to publish (R25).
    assert data["release_at"] is None

    db.session.expire_all()
    delivery = db.session.get(Delivery, delivery_id)
    order = db.session.get(Order, order_id)
    # Re-dated to local today. The window is the customer's and is kept, although
    # 09:00-11:00 is already behind the 12:00 clock: the "same-day window already past"
    # check is endpoint input validation, and nobody typed this window now (§3.6).
    assert order.delivery_date == today
    assert (order.delivery_window_start, order.delivery_window_end) == CUSTOMER_WINDOW
    # OUT_FOR_DELIVERY -> CONFIRMED is the one order move a reschedule makes (R6, R23).
    assert order.status == OrderStatus.CONFIRMED
    assert (delivery.status, delivery.delivery_person_id) == (DeliveryStatus.SCHEDULED, None)
    assert delivery.failed_delivery_reason is None
    assert delivery.delivery_attempts == 1  # the running counter survives (R8)
    assert delivery.scheduled_date.date() == today
    last = _last_history(delivery_id)
    assert (last.old_status, last.new_status, last.changed_by, last.reason) == (
        DeliveryStatus.FAILED,
        DeliveryStatus.SCHEDULED,
        admin_user.id,
        REASON,
    )
    db.session.refresh(route)
    assert route.optimized_order == []

    # Offered to drivers at once: the auto-assign timer and the pool evaluator, which
    # itself produces the new-order broadcast. Nobody else is told. A FAILED delivery
    # is on no driver's live route and owes the customer no notice (R13, R16).
    assert sorted(enqueued) == sorted(
        [
            (auto_assign_delivery_task.name, (delivery_id,), {}),
            (evaluate_pool_insertion_suggestions_task.name, (delivery_id,), {}),
        ]
    )
    assert delivery_id in _pool_ids(client, app, rostered_driver)


def test_operator_redispatch_before_todays_release_holds_it_until_the_shift_starts(
    app, client, db, operator_auth_headers, operator_user, rostered_driver, sample_user, enqueued, monkeypatch
):
    frozen = _freeze_business_clock(monkeypatch, time(7, 0))  # before the 08:00 shift start
    today = frozen.date()
    delivery, route = _failed_delivery(db, sample_user, rostered_driver, today=today, number="ORD-RD-OPER")
    delivery_id, order_id = delivery.id, delivery.order_id

    response = client.post(
        STAFF_REDISPATCH.format(delivery_id), json={"reason": "Call before coming"}, headers=operator_auth_headers
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    data = response.get_json()["data"]
    assert data["status"] == "rescheduled"
    # When drivers will see it: today's 08:00 shift start, in UTC (R25).
    assert data["release_at"] == _published_release(today)

    db.session.expire_all()
    delivery = db.session.get(Delivery, delivery_id)
    order = db.session.get(Order, order_id)
    assert order.delivery_date == today
    assert (order.delivery_window_start, order.delivery_window_end) == CUSTOMER_WINDOW
    assert order.status == OrderStatus.CONFIRMED
    assert (delivery.status, delivery.delivery_person_id) == (DeliveryStatus.RESCHEDULED, None)
    # No hours countdown on the customer's Track screen for a held delivery (§3.3).
    assert delivery.estimated_delivery_time is None
    last = _last_history(delivery_id)
    assert (last.old_status, last.new_status, last.changed_by, last.reason) == (
        DeliveryStatus.FAILED,
        DeliveryStatus.RESCHEDULED,
        operator_user.id,
        "Call before coming",
    )
    db.session.refresh(route)
    assert route.optimized_order == []

    # Held: nothing is offered to anyone until the release tick at 08:00 (R5).
    assert enqueued == []
    assert delivery_id not in _pool_ids(client, app, rostered_driver)


@pytest.mark.parametrize(
    "clock, landed, held",
    [(time(7, 0), "rescheduled", True), (time(12, 0), "scheduled", False)],
    ids=["before-todays-release", "after-todays-release"],
)
@pytest.mark.parametrize("endpoint", ["admin", "operator"])
def test_both_redispatch_responses_say_when_drivers_will_see_it(
    request, client, db, rostered_driver, sample_user, enqueued, monkeypatch, endpoint, clock, landed, held
):
    """R25, on both buttons. Before today's first shift the delivery is held, and "a driver
    can re-claim it now" would be false, so each response carries the instant drivers see
    it. After release it is in the pool already, and `release_at` is null. The key is
    always present: null means "now", never "unknown"."""
    frozen = _freeze_business_clock(monkeypatch, clock)
    delivery, _route = _failed_delivery(
        db, sample_user, rostered_driver, today=frozen.date(), number=f"ORD-RD-WHEN-{endpoint}-{landed}"
    )

    if endpoint == "admin":
        response = client.post(
            ADMIN_REDISPATCH.format(delivery.id), json={}, headers=request.getfixturevalue("admin_claim_headers")
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        data = response.get_json()["data"]
        status = data["delivery"]["status"]
    else:
        response = client.post(
            STAFF_REDISPATCH.format(delivery.id), json={}, headers=request.getfixturevalue("operator_auth_headers")
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        data = response.get_json()["data"]
        status = data["status"]

    assert status == landed
    assert "release_at" in data
    assert data["release_at"] == (_published_release(frozen.date()) if held else None)


# --------------------------------------------------------------------------- #
# 2. Refusals carry their code on both routes and write nothing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", sorted(REFUSALS))
@pytest.mark.parametrize("endpoint", ["admin", "operator"])
def test_a_refused_redispatch_answers_its_code_and_writes_nothing(
    request, client, db, rostered_driver, sample_user, enqueued, monkeypatch, endpoint, case
):
    frozen = _freeze_business_clock(monkeypatch, time(12, 0))
    delivery, _route = _failed_delivery(
        db, sample_user, rostered_driver, today=frozen.date(), number=f"ORD-RD-{endpoint}-{case}"
    )
    _make_stale(db, delivery, case)
    before = _snapshot(delivery)

    if endpoint == "admin":
        response = client.post(
            ADMIN_REDISPATCH.format(delivery.id), json={}, headers=request.getfixturevalue("admin_claim_headers")
        )
        code = (response.get_json().get("data") or {}).get("error_code")
    else:
        response = client.post(
            STAFF_REDISPATCH.format(delivery.id), json={}, headers=request.getfixturevalue("operator_auth_headers")
        )
        code = response.get_json().get("error_code")

    assert response.status_code == 400, response.get_data(as_text=True)
    assert code == REFUSALS[case]
    db.session.expire_all()
    assert _snapshot(db.session.get(Delivery, delivery.id)) == before
    assert enqueued == []


# --------------------------------------------------------------------------- #
# 3. The operator's staff-bot button, against the real route
# --------------------------------------------------------------------------- #


def test_the_operator_bot_redispatch_reaches_the_reschedule(
    app, db, http, operator_user, rostered_driver, sample_user, echo_i18n, monkeypatch
):
    frozen = _freeze_business_clock(monkeypatch, time(12, 0))
    delivery, _route = _failed_delivery(db, sample_user, rostered_driver, today=frozen.date(), number="ORD-RD-BOT")
    bridge = _OperatorBridge(http, _token(app, operator_user))
    handler = RedispatchHandler()
    _patch_handler(monkeypatch, handler, redispatch_module, bridge)
    tap, context = _make_update_context(callback_data=f"staff_redispatch_do_{delivery.id}")

    asyncio.run(handler.redispatch_delivery(tap, context))

    assert bridge.answers == [("POST", STAFF_REDISPATCH.format(delivery.id), 200, None)]
    # After release it is in the pool, so the existing copy is true and stays.
    assert tap.callback_query.edit_message_text.await_args.args[0] == "✅ staff.redispatch.success"
    assert ("staff.redispatch.success", {}) in echo_i18n
    assert "staff.redispatch.success_held" not in [key for key, _ in echo_i18n]
    db.session.expire_all()
    row = db.session.get(Delivery, delivery.id)
    assert (row.status, row.delivery_person_id, row.order.delivery_date) == (
        DeliveryStatus.SCHEDULED,
        None,
        frozen.date(),
    )


def test_the_operator_bot_says_when_a_held_redispatch_reaches_drivers(
    app, db, http, operator_user, rostered_driver, sample_user, echo_i18n, monkeypatch
):
    """R25. At 07:00 the delivery lands `rescheduled` until the 08:00 shift start. The
    operator is told that time, in Tashkent, from the UTC `release_at` the route
    published, instead of "a driver can now re-claim it"."""
    frozen = _freeze_business_clock(monkeypatch, time(7, 0))
    delivery, _route = _failed_delivery(
        db, sample_user, rostered_driver, today=frozen.date(), number="ORD-RD-BOT-HELD"
    )
    bridge = _OperatorBridge(http, _token(app, operator_user))
    handler = RedispatchHandler()
    _patch_handler(monkeypatch, handler, redispatch_module, bridge)
    tap, context = _make_update_context(callback_data=f"staff_redispatch_do_{delivery.id}")

    asyncio.run(handler.redispatch_delivery(tap, context))

    assert bridge.answers == [("POST", STAFF_REDISPATCH.format(delivery.id), 200, None)]
    assert tap.callback_query.edit_message_text.await_args.args[0] == "✅ staff.redispatch.success_held"
    assert ("staff.redispatch.success_held", {"time": "08:00"}) in echo_i18n
    assert "staff.redispatch.success" not in [key for key, _ in echo_i18n]
    db.session.expire_all()
    row = db.session.get(Delivery, delivery.id)
    assert (row.status, row.delivery_person_id, row.order.delivery_date) == (
        DeliveryStatus.RESCHEDULED,
        None,
        frozen.date(),
    )


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_the_operator_bot_reads_a_refused_redispatch_as_a_conflict(
    app, db, http, operator_user, rostered_driver, sample_user, echo_i18n, monkeypatch, case
):
    """Every refusal an operator can meet here is a 400 now, and an unmapped 400 renders
    "check the entered data" to someone who typed nothing: the row changed under their
    card. They get the conflict sentence, the one a 409 STAFF_ORDER_NOT_ACTIVE produced."""
    frozen = _freeze_business_clock(monkeypatch, time(12, 0))
    delivery, _route = _failed_delivery(
        db, sample_user, rostered_driver, today=frozen.date(), number=f"ORD-RD-BOT-{case}"
    )
    _make_stale(db, delivery, case)
    bridge = _OperatorBridge(http, _token(app, operator_user))
    handler = RedispatchHandler()
    _patch_handler(monkeypatch, handler, redispatch_module, bridge)
    tap, context = _make_update_context(callback_data=f"staff_redispatch_do_{delivery.id}")

    asyncio.run(handler.redispatch_delivery(tap, context))

    assert bridge.answers == [("POST", STAFF_REDISPATCH.format(delivery.id), 400, REFUSALS[case])]
    tap.callback_query.answer.assert_awaited_once_with("❌ staff.error.api.conflict", show_alert=True)
    tap.callback_query.edit_message_text.assert_not_called()


# --------------------------------------------------------------------------- #
# 4. The re-check under the lock, the one that counts
# --------------------------------------------------------------------------- #


def test_the_locked_recheck_refuses_a_row_a_driver_claimed_after_the_unlocked_read(
    db, admin_user, rostered_driver, sample_user, enqueued, monkeypatch
):
    """`redispatch_failed_delivery` reads FAILED unlocked, as a fast path only. Between
    that read and `reschedule`'s row lock, a colleague can re-dispatch the same row and a
    driver can claim the re-offer. Without the re-check on the locked row, this second
    re-dispatch would take the stop off the driver who just accepted it. So `reschedule`
    is called here the way the fast path hands it over, on the row as it is by then:
    ASSIGNED to that driver."""
    frozen = _freeze_business_clock(monkeypatch, time(12, 0))
    delivery, _route = _failed_delivery(
        db, sample_user, rostered_driver, today=frozen.date(), number="ORD-RD-CLAIMED"
    )
    delivery.status = DeliveryStatus.ASSIGNED
    delivery.delivery_person_id = rostered_driver.id
    delivery.failed_delivery_reason = None
    delivery.order.status = OrderStatus.CONFIRMED
    db.session.commit()
    order = delivery.order
    before = _snapshot(delivery)

    with pytest.raises(ValidationError) as refused:
        OrderScheduleService.reschedule(
            order.id,
            delivery_date=frozen.date(),
            window_start=order.delivery_window_start,
            window_end=order.delivery_window_end,
            actor_user_id=admin_user.id,
            expected_delivery_status=DeliveryStatus.FAILED,
        )

    assert refused.value.error_code == "STAFF_DELIVERY_NOT_REDISPATCHABLE"
    db.session.expire_all()
    row = db.session.get(Delivery, delivery.id)
    # The driver keeps the stop they claimed, and nothing else moved.
    assert (row.status, row.delivery_person_id) == (DeliveryStatus.ASSIGNED, rostered_driver.id)
    assert DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id).count() == 0
    assert _snapshot(row) == before
    assert enqueued == []
