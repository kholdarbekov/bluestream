"""F5: a customer may cancel their own order only while nothing is paid and the driver has not
picked it up.

Spec: docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md §3.3 and
§8 item 3, as amended 2026-10-21 (owner): the cutoff moved from the driver's accept to the
driver's "Picked up" tap; `preparing` is cancellable; an `out_for_delivery` an admin set by
hand stays refused; the driver is sent no message. Amended 2026-10-22 (owner): the pinned route
card of a driver who held the stop is refreshed silently, with no sound and no alert.

Order 1277 (TG_000508_26) is why. Its Cancel button was drawn while the order was pending;
the customer tapped it after the delivery had failed, and it ended an order that was only
waiting for a new date. The rule is now one backend predicate,
`OrderService.customer_cancel_block_code`. `GET /orders` and `GET /orders/<id>` publish it
as `can_customer_cancel`, and `POST /orders/<id>/cancel` enforces it on locked rows. Every
test drives those routes the way the bot and the web call them: neither client sends a
body with the cancel POST.
"""

import inspect
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Callable, NamedTuple, Optional

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy.orm.attributes import set_committed_value

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute, DeliveryStatusHistory
from business_app.models.order import Order, OrderStatusHistory
from business_app.models.payment import Payment
from business_app.models.translation import Translation
from business_app.services.notification_service import NotificationService
from business_app.services.route_optimization_service import RouteOptimizationService
from business_app.tasks import staff_tasks
from business_app.tasks.notification_tasks import send_order_notification_task
from business_app.utils.constants import NotificationType
from business_app.utils.state_validators import DELIVERY_DRIVERLESS_STATES
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, PaymentStatus
from tests.unit.test_delivery_service_business_rules import _task_spy

ORDERS_URL = "/api/v1/orders/"
ORDER_URL = "/api/v1/orders/{order_id}"
CANCEL_URL = "/api/v1/orders/{order_id}/cancel"
DRIVER_ACCEPT_URL = "/api/v1/staff/delivery/accept/{delivery_id}"
DRIVER_STATUS_URL = "/api/v1/staff/delivery/{delivery_id}/status"
ADMIN_DELIVERY_URL = "/api/v1/admin/deliveries/{delivery_id}"
BULK_ACTIONS_URL = "/api/v1/admin/bulk-actions"
STAFF_PREPARING_URL = "/api/v1/staff/orders/{order_id}/preparing"
LEGACY_START_DELIVERY_URL = "/api/v1/delivery/driver/start-delivery/{delivery_id}"
REFUSED = "ORDER_NOT_CUSTOMER_CANCELLABLE"
# The customer's own status message after a cancel: `customer_told` intercepts it and its
# assertions pin it, so `staff_told` leaves it alone. Nothing else may be published.
CUSTOMER_NOTICE_TASKS = (send_order_notification_task,)
# The one thing a cancel publishes for staff (owner, 2026-10-22): a silent refresh of the pinned
# route card of the driver who held the stop, published after the cancel commits. Named, not
# imported, so a missing task fails the assertions below rather than the whole module.
CARD_REFRESH_TASK = "business_app.tasks.delivery_tasks.refresh_driver_route_card_task"

# A link no config default produces, so finding it in the message proves the route read
# COMPANY_TELEGRAM_BOT_URL when it answered.
BOT_URL = "https://t.me/cancel_refusal_probe_bot"

# The web copy each published reason_code is rendered from (the plan's interface contract).
REFUSAL_COPY_KEY = {
    "NOT_CANCELLABLE": "api.orders.cancel_refused.not_cancellable",
    "AWAITING_NEW_DATE": "api.orders.cancel_refused.awaiting_new_date",
    "PAID": "api.orders.cancel_refused.paid",
    "WITH_DRIVER": "api.orders.cancel_refused.with_driver",
}


class Case(NamedTuple):
    name: str
    order_status: OrderStatus
    is_paid: bool
    payment_status: PaymentStatus
    delivery_status: Optional[DeliveryStatus]
    refused: Optional[str]  # the published reason_code; None when the customer may cancel
    # Where an allowed cancel leaves the delivery. The cascade cancels a live row and leaves a
    # final one as it was.
    delivery_after: Optional[DeliveryStatus] = DeliveryStatus.CANCELLED


# Short names keep each row of the matrix on one line.
OS, PS, DS = OrderStatus, PaymentStatus, DeliveryStatus

CASES = [
    Case("pending-unpaid", OS.PENDING, False, PS.PENDING, None, None),
    Case("pending-paid", OS.PENDING, True, PS.COMPLETED, None, "PAID"),
    Case("confirmed-no-delivery-row", OS.CONFIRMED, False, PS.PENDING, None, None),
    # Nothing has left with a driver: the delivery is in the pool, held for a later day, or
    # accepted by a driver who has not tapped Picked up yet.
    Case("confirmed-scheduled", OS.CONFIRMED, False, PS.PENDING, DS.SCHEDULED, None),
    Case("confirmed-in-the-pool", OS.CONFIRMED, False, PS.PENDING, DS.PENDING, None),
    Case("confirmed-held-for-a-later-day", OS.CONFIRMED, False, PS.PENDING, DS.RESCHEDULED, None),
    Case("confirmed-driver-accepted", OS.CONFIRMED, False, PS.PENDING, DS.ASSIGNED, None),
    # Owner decision 1 (2026-10-21): `preparing` no longer blocks on its own.
    Case("preparing-no-delivery-row", OS.PREPARING, False, PS.PENDING, None, None),
    Case("preparing-in-the-pool", OS.PREPARING, False, PS.PENDING, DS.PENDING, None),
    Case("preparing-driver-accepted", OS.PREPARING, False, PS.PENDING, DS.ASSIGNED, None),
    # The driver tapped Picked up, or a later step. The confirmed and preparing rows show that
    # the delivery alone refuses, whatever the order's own status says.
    Case("confirmed-picked-up", OS.CONFIRMED, False, PS.PENDING, DS.PICKED_UP, "WITH_DRIVER"),
    Case("preparing-picked-up", OS.PREPARING, False, PS.PENDING, DS.PICKED_UP, "WITH_DRIVER"),
    Case("out-for-delivery-picked-up", OS.OUT_FOR_DELIVERY, False, PS.PENDING, DS.PICKED_UP, "WITH_DRIVER"),
    Case("confirmed-in-transit", OS.CONFIRMED, False, PS.PENDING, DS.IN_TRANSIT, "WITH_DRIVER"),
    Case("out-for-delivery", OS.OUT_FOR_DELIVERY, False, PS.PENDING, DS.IN_TRANSIT, "WITH_DRIVER"),
    Case("confirmed-arrived", OS.CONFIRMED, False, PS.PENDING, DS.ARRIVED, "WITH_DRIVER"),
    Case("out-for-delivery-arrived", OS.OUT_FOR_DELIVERY, False, PS.PENDING, DS.ARRIVED, "WITH_DRIVER"),
    # Owner decision 2: an admin moved the order to out_for_delivery by hand on the Orders page,
    # with no pickup. It stays refused.
    Case("out-for-delivery-set-by-hand", OS.OUT_FOR_DELIVERY, False, PS.PENDING, DS.ASSIGNED, "WITH_DRIVER"),
    Case("out-for-delivery-set-by-hand-no-delivery-row", OS.OUT_FOR_DELIVERY, False, PS.PENDING, None, "WITH_DRIVER"),
    # A pending order whose delivery is in the picked-up band is refused: the owner's cutoff.
    Case("pending-picked-up", OS.PENDING, False, PS.PENDING, DS.PICKED_UP, "WITH_DRIVER"),
    # Fails closed past PENDING: a live order whose delivery row sits in a leftover final state
    # is refused (order 153 / TG_000085_26 is such a row in the dev DB). Nobody picked it up, so
    # the refusal is the generic one, never "the driver has picked it up".
    Case("confirmed-leftover-cancelled-delivery", OS.CONFIRMED, False, PS.PENDING, DS.CANCELLED, "NOT_CANCELLABLE"),
    Case("confirmed-leftover-delivered-delivery", OS.CONFIRMED, False, PS.PENDING, DS.DELIVERED, "NOT_CANCELLABLE"),
    Case("confirmed-leftover-returned-delivery", OS.CONFIRMED, False, PS.PENDING, DS.RETURNED, "NOT_CANCELLABLE"),
    Case("preparing-leftover-returned-delivery", OS.PREPARING, False, PS.PENDING, DS.RETURNED, "NOT_CANCELLABLE"),
    # A PENDING order keeps today's answer (controller ruling, 2026-10-21): an order an admin
    # moved returned -> pending keeps its `returned` delivery and stays cancellable. The cascade
    # leaves a final delivery row as it was.
    Case("pending-leftover-returned-delivery", OS.PENDING, False, PS.PENDING, DS.RETURNED, None, DS.RETURNED),
    Case("pending-leftover-cancelled-delivery", OS.PENDING, False, PS.PENDING, DS.CANCELLED, None, DS.CANCELLED),
    # A failed stop on a live order waits for a new date, which is checked before the driver.
    Case("awaiting-new-date-confirmed", OS.CONFIRMED, False, PS.PENDING, DS.FAILED, "AWAITING_NEW_DATE"),
    Case("awaiting-new-date-out-for-delivery", OS.OUT_FOR_DELIVERY, False, PS.PENDING, DS.FAILED, "AWAITING_NEW_DATE"),
    # An order moved returned -> pending over a still-failed delivery. The leftover check does not
    # apply to PENDING, so this check is the only thing that refuses it.
    Case("awaiting-new-date-pending", OS.PENDING, False, PS.PENDING, DS.FAILED, "AWAITING_NEW_DATE"),
    # Awaiting is checked before paid: the customer hears the more specific true thing.
    Case("awaiting-new-date-and-paid", OS.CONFIRMED, True, PS.COMPLETED, DS.FAILED, "AWAITING_NEW_DATE"),
    Case("partially-paid", OS.CONFIRMED, False, PS.PARTIALLY_PAID, None, "PAID"),
    # The payment went through before `is_paid` was stamped: the money is still in.
    Case("completed-payment-not-yet-stamped", OS.CONFIRMED, False, PS.COMPLETED, None, "PAID"),
    # Checked first of all, so a delivered order is "not cancellable", never "paid".
    Case("delivered", OS.DELIVERED, True, PS.COMPLETED, DS.DELIVERED, "NOT_CANCELLABLE"),
]

# The allowed cancels of a stop a driver had accepted: the only rows whose cancel reaches a
# driver, through one silent refresh of that driver's route card (owner, 2026-10-22). A
# driverless delivery, and a final row the cascade leaves as it was, refresh nobody.
HELD_BY_THE_DRIVER = {"confirmed-driver-accepted", "preparing-driver-accepted"}


def _headers(app, user):
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _seed(db, customer, driver, case):
    order = Order(
        user_id=customer.id,
        order_number=f"ORD-F5-{case.name}",
        status=case.order_status,
        payment_method=PaymentMethod.CLICK,
        is_paid=case.is_paid,
        subtotal=Decimal("15000.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("15000.00"),
    )
    db.session.add(order)
    db.session.flush()
    collected = {PS.COMPLETED: Decimal("15000.00"), PS.PARTIALLY_PAID: Decimal("5000.00")}.get(
        case.payment_status, Decimal("0.00")
    )
    db.session.add(
        Payment(
            order_id=order.id,
            user_id=customer.id,
            payment_method=PaymentMethod.CLICK,
            amount=Decimal("15000.00"),
            amount_collected=collected,
            outstanding_amount=Decimal("15000.00") - collected,
            currency="UZS",
            status=case.payment_status,
            payment_id=f"PAY-F5-{case.name}",
        )
    )
    delivery = None
    if case.delivery_status is not None:
        delivery = Delivery(
            order_id=order.id,
            status=case.delivery_status,
            # The CHECK constraint's own set: a driverless row never carries a driver.
            delivery_person_id=None if case.delivery_status in DELIVERY_DRIVERLESS_STATES else driver.id,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
    db.session.commit()
    return order, delivery


@pytest.fixture
def refusal_copy(app, db, monkeypatch):
    """Seed the web refusal copy exactly as the canonical seeder ships it, and pin the bot link.

    The test DB carries no translation rows, so without this `get_translation` returns the
    raw key and the message could not be checked. Only the `api.orders.cancel_refused.*`
    rows the seeder owns are loaded, so a route asking for any key the seeder does not ship
    still gets the raw key back and fails below."""
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for

    monkeypatch.setitem(app.config, "COMPANY_TELEGRAM_BOT_URL", BOT_URL)
    copy = {key: texts for key, texts in BACKEND_TRANSLATIONS.items() if key.startswith("api.orders.cancel_refused.")}
    for key, texts in copy.items():
        for language, value in texts.items():
            db.session.add(Translation(key=key, language=language, value=value, category=_category_for(key)))
    db.session.commit()
    return copy


@pytest.fixture
def customer_told(monkeypatch):
    """Everything the cancel tells the customer, with its exact arguments.

    A successful cancel queues the status message after its commit, and the route sends its
    own notification. A refusal must do neither. The status message is caught on both publish
    paths, `.delay` and `.apply_async`, each bound against the task's own signature, so it lands in
    the same tuple whichever one the code uses."""
    from business_app.services.notification_service import NotificationService

    told = []

    # Mirrors NotificationService.send_notification exactly, like the autouse stub in
    # tests/conftest.py, so a drifted call fails here instead of being swallowed.
    def _send_notification(
        _self,
        user_id,
        notification_type,
        channels=None,
        template_data=None,
        priority="normal",
        template_override=None,
        campaign_id=None,
        email_headers=None,
    ):
        told.append(("notification", str(user_id), notification_type, (template_data or {}).get("order_number")))
        return {}

    status_message = inspect.signature(send_order_notification_task.run)

    def _queue_status_message(*args, **kwargs):
        bound = status_message.bind(*args, **kwargs)
        told.append(("status_message", bound.arguments["order_id"], bound.arguments["notification_type"]))

    def _queue_status_message_async(args=None, kwargs=None, **_options):
        _queue_status_message(*(args or ()), **(kwargs or {}))

    monkeypatch.setattr(NotificationService, "send_notification", _send_notification, raising=True)
    monkeypatch.setitem(vars(send_order_notification_task), "delay", _queue_status_message)
    monkeypatch.setitem(vars(send_order_notification_task), "apply_async", _queue_status_message_async)
    return told


class _StaffTold:
    """Everything a request publishes to Celery or posts to the staff bot, beyond the customer's
    own notices.

    Three seams reach a driver, and all three are watched:
    - Celery. Every registered task's `.delay` and `.apply_async` is recorded by `_task_spy`, which
      binds each call against the task's own signature: the `staff_tasks` pushes, and also
      `delivery_tasks.optimize_driver_route_task`, which posts route-updated. The task modules are
      imported first, so a task a request would import lazily is registered, and watched, too.
    - The two webhook helpers: `staff_tasks._send_staff_webhook` and
      `bot_webhook._send_staff_bot_webhook` (route-updated, pool-insertion offers).
    - The direct staff-bot Telegram send, `NotificationService.send_staff_telegram_message`.
    Each helper and the direct send is replaced by a fake with its exact signature.

    The one task left alone is the customer's status message (`CUSTOMER_NOTICE_TASKS`), which
    `customer_told` intercepts and its own assertions pin. Anything else published or posted shows
    up in `everything()`."""

    def __init__(self, monkeypatch):
        import importlib
        import pkgutil

        from celery import current_app as celery_app

        import business_app.tasks as task_modules
        from business_app.utils import bot_webhook

        for module in pkgutil.iter_modules(task_modules.__path__):
            if module.name.endswith("_tasks"):
                importlib.import_module(f"{task_modules.__name__}.{module.name}")
        left_to_customer_told = {task.name for task in CUSTOMER_NOTICE_TASKS}
        self._published = [
            (task.name, method, _task_spy(monkeypatch, task, method))
            for task in list(celery_app.tasks.values())
            if task.name not in left_to_customer_told
            for method in ("delay", "apply_async")
        ]
        watched = {name for name, _method, _calls in self._published}
        assert {"staff.notify_order_cancelled", "staff.notify_order_unassigned"} <= watched, watched
        assert any(name.endswith("optimize_driver_route_task") for name in watched), watched
        self._posted = []

        def _send_staff_webhook(endpoint: str, data: dict, timeout: int = 10) -> bool:
            self._posted.append(("staff_tasks", endpoint, data))
            return True

        def _send_staff_bot_webhook(endpoint: str, payload: dict, *, timeout: float = 10.0) -> bool:
            self._posted.append(("bot_webhook", endpoint, payload))
            return True

        def _send_staff_telegram_message(_service, user, message: str, *, language=None) -> dict:
            self._posted.append(("staff_telegram", getattr(user, "id", None), message))
            return {"success": True}

        monkeypatch.setattr(staff_tasks, "_send_staff_webhook", _send_staff_webhook, raising=True)
        monkeypatch.setattr(bot_webhook, "_send_staff_bot_webhook", _send_staff_bot_webhook, raising=True)
        monkeypatch.setattr(
            NotificationService, "send_staff_telegram_message", _send_staff_telegram_message, raising=True
        )

    def everything(self):
        return [(name, method, call) for name, method, calls in self._published for call in calls] + list(
            self._posted
        )

    def forget(self):
        for _name, _method, calls in self._published:
            calls.clear()
        self._posted.clear()


@pytest.fixture
def staff_told(monkeypatch, customer_told):
    """Owner decisions: a customer's cancel sends the driver no message (2026-10-21), and the
    driver who held the stop gets one silent refresh of their pinned route card (2026-10-22),
    `_card_refresh` below. Nothing else reaches staff.

    Set up after `customer_told`, which keeps its own fake on the customer's status message."""
    return _StaffTold(monkeypatch)


def _card_refresh(driver_id, delivery_id):
    """What `staff_told` records for the one silent route-card refresh a cancel publishes."""
    return (CARD_REFRESH_TASK, "delay", ((driver_id, delivery_id), {}))


def _run_card_refresh_as_the_worker(db, call, task_id):
    """Run one recorded `_card_refresh` publish as the worker would: its own arguments, a fresh
    read, and a Celery request carrying `task_id`, which the push keys its `event_id` on."""
    from celery import current_app as celery_app

    task = celery_app.tasks[CARD_REFRESH_TASK]
    _name, _method, (args, kwargs) = call
    db.session.expire_all()
    task.push_request(id=task_id)
    try:
        return task.run(*args, **kwargs)
    finally:
        task.pop_request()


def _route_updated_push(posted):
    """The staff-bot route-updated posts in `posted`, each with its timestamp checked and removed:
    the clock is the only value a test cannot name."""
    pushes = []
    for source, endpoint, payload in posted:
        payload = dict(payload)
        datetime.fromisoformat(payload.pop("timestamp"))
        pushes.append((source, endpoint, payload))
    return pushes


def _delivery_untouched(delivery):
    """What a cascade that leaves a final delivery row alone must not change."""
    return (
        DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id).count(),
        delivery.delivery_person_id,
        delivery.updated_at,
    )


def _on_duty(db, driver):
    """`driver` with the active DeliveryPerson profile the staff routes look up by users.id."""
    db.session.add(
        DeliveryPerson(
            user_id=driver.id,
            full_name=f"{driver.first_name} {driver.last_name}",
            phone=driver.phone,
            is_active=True,
            is_available=True,
        )
    )
    db.session.commit()
    return driver


def _assert_web_copy(app, message, seeded):
    """The web shows `message` as it is: the seeded copy with the bot link filled in, and
    never a phone number (F16)."""
    assert message in {text.format(bot_url=BOT_URL) for text in seeded.values()}, message
    assert BOT_URL in message
    assert app.config["COMPANY_PHONE"] not in message
    assert not re.search(r"\d{5,}|tel:", message), message


@pytest.mark.integration
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_the_cancel_a_customer_is_offered_is_the_cancel_the_backend_allows(
    app, client, db, sample_user, delivery_driver, refusal_copy, customer_told, staff_told, case
):
    order, delivery = _seed(db, sample_user, delivery_driver, case)
    headers = _headers(app, sample_user)

    detail = client.get(ORDER_URL.format(order_id=order.id), headers=headers)
    assert detail.status_code == 200, detail.get_json()
    published = detail.get_json()["data"]["order"]
    assert "error" not in published, f"the degraded fallback answered, so its False proves nothing: {published}"
    listed = client.get(ORDERS_URL, headers=headers).get_json()["data"]["orders"]
    assert [row["can_customer_cancel"] for row in listed if row["id"] == order.id] == [
        published["can_customer_cancel"]
    ], "the list the web draws Cancel from must agree with the detail"
    # A row the cascade must leave as it was (a final row under an allowed PENDING cancel):
    # its history, its driver and its timestamp, read before the POST.
    left_alone = case.refused is None and delivery is not None and case.delivery_after is case.delivery_status
    before = _delivery_untouched(delivery) if left_alone else None

    response = client.post(CANCEL_URL.format(order_id=order.id), headers=headers)
    body = response.get_json()
    db.session.expire_all()

    # The button a customer is shown and the write it sends agree, in every row.
    assert published["can_customer_cancel"] is (response.status_code == 200), body

    if case.refused is None:
        assert response.status_code == 200, body
        assert body["data"]["order"]["status"] == "cancelled"
        assert body["data"]["order"]["can_customer_cancel"] is False
        assert db.session.get(Order, order.id).status is OrderStatus.CANCELLED
        if delivery is not None:
            assert db.session.get(Delivery, delivery.id).status is case.delivery_after
        if left_alone:
            assert _delivery_untouched(db.session.get(Delivery, delivery.id)) == before
        assert ("status_message", order.id, "status_changed_cancelled") in customer_told
        assert ("notification", str(sample_user.id), NotificationType.ORDER_UPDATE, order.order_number) in customer_told
        # The driver who had accepted the order gets no message (owner, 2026-10-21), only one
        # silent refresh of their route card (owner, 2026-10-22).
        if case.name in HELD_BY_THE_DRIVER:
            assert delivery.delivery_person_id == delivery_driver.id
            assert staff_told.everything() == [_card_refresh(delivery_driver.id, delivery.id)]
        else:
            assert staff_told.everything() == []
    else:
        assert response.status_code == 400, body
        assert body["success"] is False
        assert body["data"] == {"error_code": REFUSED, "reason_code": case.refused, "can_customer_cancel": False}
        _assert_web_copy(app, body["message"], refusal_copy[REFUSAL_COPY_KEY[case.refused]])
        # Refused before anything moved: the order, its delivery and its money are as they were,
        # and the customer is told nothing beyond the refusal itself.
        assert db.session.get(Order, order.id).status is case.order_status
        if delivery is not None:
            assert db.session.get(Delivery, delivery.id).status is case.delivery_status
        assert Payment.query.filter_by(order_id=order.id).one().status is case.payment_status
        assert customer_told == []
        assert staff_told.everything() == []


@pytest.mark.integration
def test_a_driver_pickup_this_session_has_not_seen_is_still_refused(
    app, client, db, sample_user, delivery_driver, customer_told, staff_told
):
    """The customer's page was drawn while the driver had accepted the order but not picked it
    up, so it offered Cancel. A moment ago the driver tapped Picked up on another connection.
    The pickup (`StaffService.update_delivery_status`) locks the Delivery and then writes the
    order, so the decision must be made on that row, locked and re-read, never on the copy this
    session already holds. Otherwise the cancel cascade would end an order that is on its way.

    The pickup goes through the driver's real route. The session's stale picture is then built
    deterministically: both instances in the identity map are put back to what this session last
    read, as committed state, so only a `populate_existing` re-read can see past them. It is the
    technique of
    test_admin_order_reschedule.py::test_the_reschedule_decides_on_the_row_it_locked_not_a_stale_copy,
    committed here because a refusal rolls back and would otherwise undo the pickup too."""
    case = Case("stale-read", OS.CONFIRMED, False, PS.PENDING, DS.ASSIGNED, "WITH_DRIVER")
    order, delivery = _seed(db, sample_user, delivery_driver, case)
    stale = db.session.get(Order, order.id)
    assert stale.delivery is delivery and delivery.status is DeliveryStatus.ASSIGNED

    picked_up = client.put(
        DRIVER_STATUS_URL.format(delivery_id=delivery.id),
        json={"status": "picked_up"},
        headers=_headers(app, delivery_driver),
    )
    assert picked_up.status_code == 200, picked_up.get_json()
    assert picked_up.get_json()["data"]["status"] == "picked_up"
    # The pickup's commit refreshed both rows; put the stale reads back.
    assert (stale.status, delivery.status) == (OrderStatus.OUT_FOR_DELIVERY, DeliveryStatus.PICKED_UP)
    set_committed_value(stale, "status", OrderStatus.CONFIRMED)
    set_committed_value(delivery, "status", DeliveryStatus.ASSIGNED)
    assert db.session.get(Delivery, delivery.id) is delivery and delivery.status is DeliveryStatus.ASSIGNED
    customer_told.clear()
    staff_told.forget()

    response = client.post(CANCEL_URL.format(order_id=order.id), headers=_headers(app, sample_user))

    body = response.get_json()
    assert response.status_code == 400, body
    assert body["data"] == {"error_code": REFUSED, "reason_code": "WITH_DRIVER", "can_customer_cancel": False}
    db.session.expire_all()
    landed = db.session.get(Delivery, delivery.id)
    assert (landed.status, landed.delivery_person_id) == (DeliveryStatus.PICKED_UP, delivery_driver.id)
    assert db.session.get(Order, order.id).status is OrderStatus.OUT_FOR_DELIVERY
    assert customer_told == []
    assert staff_told.everything() == []


@pytest.mark.integration
def test_after_the_driver_accepts_the_customer_still_cancels_and_the_drivers_card_is_refreshed_silently(
    app, client, db, sample_user, delivery_driver, customer_told, staff_told
):
    """F5 as amended 2026-10-21 and 2026-10-22. A driver accepts the order from the pool through
    the real staff route, and the stop sits on the driver's route for today. The order still offers
    Cancel, and the customer's cancel goes through: the order and its delivery end `cancelled`, the
    stop leaves the driver's route and the driver's active count drops back to zero.

    The driver gets no message (owner, 2026-10-21). Their pinned route card is refreshed silently
    (owner, 2026-10-22): one refresh is published after the cancel commits, and the worker running
    it posts one route-updated push to the staff bot with `sound: False`, the `order_cancelled`
    trigger and the cancelled stop, and no materiality keys, which only a re-solve computes."""
    case = Case("accepted-then-cancelled", OS.CONFIRMED, False, PS.PENDING, DS.PENDING, None)
    order, delivery = _seed(db, sample_user, delivery_driver, case)
    customer_headers = _headers(app, sample_user)

    accepted = client.post(
        DRIVER_ACCEPT_URL.format(delivery_id=delivery.id), headers=_headers(app, _on_duty(db, delivery_driver))
    )
    assert accepted.status_code == 200, accepted.get_json()
    assert accepted.get_json()["data"]["status"] == "assigned"
    db.session.expire_all()
    profile = DeliveryPerson.query.filter_by(user_id=delivery_driver.id).one()
    assert (db.session.get(Delivery, delivery.id).status, profile.current_active_deliveries) == (
        DeliveryStatus.ASSIGNED,
        1,
    )
    route = RouteOptimizationService().current_route(delivery_driver.id) or DeliveryRoute(
        name="Today",
        delivery_person_id=delivery_driver.id,
        start_location_lat=41.3111,
        start_location_lng=69.2797,
        route_date=datetime.now(UTC),
    )
    route.optimized_order = [delivery.id]
    db.session.add(route)
    db.session.commit()
    published = client.get(ORDER_URL.format(order_id=order.id), headers=customer_headers)
    assert published.get_json()["data"]["order"]["can_customer_cancel"] is True
    customer_told.clear()
    staff_told.forget()

    response = client.post(CANCEL_URL.format(order_id=order.id), headers=customer_headers)

    body = response.get_json()
    assert response.status_code == 200, body
    assert (body["data"]["order"]["status"], body["data"]["order"]["can_customer_cancel"]) == ("cancelled", False)
    db.session.expire_all()
    assert db.session.get(Order, order.id).status is OrderStatus.CANCELLED
    landed = db.session.get(Delivery, delivery.id)
    assert landed.status is DeliveryStatus.CANCELLED
    last = (
        DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id).order_by(DeliveryStatusHistory.id.desc()).first()
    )
    assert (last.old_status, last.new_status) == (DeliveryStatus.ASSIGNED, DeliveryStatus.CANCELLED)
    assert DeliveryPerson.query.filter_by(user_id=delivery_driver.id).one().current_active_deliveries == 0
    assert RouteOptimizationService().current_route(delivery_driver.id).optimized_order == []
    # The customer hears exactly what any successful cancel tells them. The driver's card is
    # queued for one silent refresh, and nothing else is published or posted.
    assert sorted(customer_told, key=repr) == sorted(
        [
            ("status_message", order.id, "status_changed_cancelled"),
            ("notification", str(sample_user.id), NotificationType.ORDER_UPDATE, order.order_number),
        ],
        key=repr,
    )
    [refresh] = staff_told.everything()
    assert refresh == _card_refresh(delivery_driver.id, delivery.id)

    # The worker runs it for a driver linked to the staff bot (the fixture driver has no
    # telegram_id, and with none the push is skipped before anything is posted).
    delivery_driver.telegram_id = 700000101
    db.session.commit()
    staff_told.forget()
    pushed = _run_card_refresh_as_the_worker(db, refresh, task_id="card-refresh-1")

    assert pushed is True
    assert _route_updated_push(staff_told.everything()) == [
        (
            "bot_webhook",
            "/internal/route-updated",
            {
                "driver_id": delivery_driver.id,
                "telegram_id": 700000101,
                "event_id": "route_updated:card-refresh-1",
                "sound": False,
                "trigger": "order_cancelled",
                "delivery_id": delivery.id,
            },
        )
    ]


class StaleWriter(NamedTuple):
    """A staff or admin write that read the order before the customer's cancel committed, and the
    exact refusal it must meet instead of writing over the cancel."""

    name: str
    send: Callable  # (client, headers, order_id, delivery_id) -> response
    headers: str  # the fixture that signs the request
    status_code: int
    observed: Callable  # (response body) -> the part of it the refusal is pinned on
    expected: Callable  # (order_id, delivery_id) -> what that part must equal


def _admin_delivery_page(status, **extra):
    """`PUT /admin/deliveries/<id>` as Delivery.js `handleUpdateSubmit` sends it: the picked status
    beside the row's notes, plus the internal reason a move to returned asks for."""

    def send(client, headers, _order_id, delivery_id):
        return client.put(
            ADMIN_DELIVERY_URL.format(delivery_id=delivery_id),
            json={"status": status, "notes": None, **extra},
            headers=headers,
        )

    return send


def _bulk(action, target_type):
    """`POST /admin/bulk-actions` with the four keys `adminService.performBulkAction` sends."""

    def send(client, headers, order_id, delivery_id):
        target = order_id if target_type == "order" else delivery_id
        return client.post(
            BULK_ACTIONS_URL,
            json={"action": action, "target_type": target_type, "target_ids": [target], "parameters": {}},
            headers=headers,
        )

    return send


def _mark_preparing(client, headers, order_id, _delivery_id):
    return client.put(STAFF_PREPARING_URL.format(order_id=order_id), headers=headers)


def _one_failed_item(key, error):
    return lambda order_id, delivery_id: {
        "success_count": 0,
        "failed_count": 1,
        "errors": [{key: {"order_id": order_id, "delivery_id": delivery_id}[key], "error": error}],
        "total_errors": 1,
    }


def _admin_page_errors(body):
    return body["errors"], body.get("data")


STALE_WRITERS = [
    StaleWriter(
        "admin-delivery-page-picked-up",
        _admin_delivery_page("picked_up"),
        "admin_claim_headers",
        400,
        _admin_page_errors,
        lambda _o, _d: (["Cannot transition delivery from cancelled to picked_up. Allowed transitions: none"], None),
    ),
    StaleWriter(
        "admin-delivery-page-returned",
        _admin_delivery_page("returned", reason="Customer refused at the door"),
        "admin_claim_headers",
        400,
        _admin_page_errors,
        lambda _o, _d: (["Cannot transition delivery from cancelled to returned. Allowed transitions: none"], None),
    ),
    StaleWriter(
        "admin-bulk-mark-in-transit",
        _bulk("mark_in_transit", "delivery"),
        "admin_claim_headers",
        200,
        lambda body: body["data"]["results"],
        _one_failed_item("delivery_id", "Cannot start delivery at the current stage"),
    ),
    StaleWriter(
        "operator-mark-preparing",
        _mark_preparing,
        "operator_auth_headers",
        400,
        lambda body: (body["error_code"], body["message"]),
        lambda _o, _d: (
            "STAFF_ORDER_STATUS_INVALID_FOR_PREPARING",
            "Order must be in 'confirmed' status to mark as preparing. Current status: cancelled",
        ),
    ),
    StaleWriter(
        "admin-bulk-process",
        _bulk("process", "order"),
        "admin_claim_headers",
        200,
        lambda body: body["data"]["results"],
        _one_failed_item("order_id", f"Cannot process order with status {OrderStatus.CANCELLED}"),
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize("writer", STALE_WRITERS, ids=[writer.name for writer in STALE_WRITERS])
def test_a_write_that_read_the_order_before_the_customer_cancelled_cannot_bring_it_back(
    request, app, client, db, sample_user, delivery_driver, customer_told, staff_told, writer
):
    """The races the new cutoff opens (controller rulings, 2026-10-21). Since a customer may
    cancel an order a driver has accepted, every staff or admin write that can act on such an order
    must decide on the row as it is now, not on a copy read before the cancel committed:
    - the admin Delivery page (`AdminDeliveryService.update_delivery`) locks and re-reads the
      Delivery on entry, for Picked up, Returned and every other branch;
    - `DeliveryService.begin_delivery_in_transit` (admin bulk "mark in transit", the legacy driver
      start-delivery) locks and re-reads the Delivery;
    - operator/driver "Mark preparing" and admin bulk "process" lock and re-read the Order alone
      before their `confirmed` check (they only read the delivery, so they stay out of the
      Delivery/Order lock-order question; see spec §10).

    Each writer's stale read is built as in the tests above: after the cancel commits, the
    identity map is put back to what that request would have read first. Each then meets the
    refusal it gives any cancelled order, writes nothing, and tells no one.

    SQLite ignores FOR UPDATE, so these cases pin each writer's re-read, not its row lock.
    Real-Postgres interleavings, in test_customer_cancel_races_pg.py, exist only for the
    admin Delivery page (Picked up, Returned), the driver's Picked up route, and bulk "process"
    (its Order lock, and that a refused item releases it). No interleaving test pins the locks in
    `begin_delivery_in_transit` or `mark_order_preparing`."""
    headers = request.getfixturevalue(writer.headers)
    case = Case(f"stale-{writer.name}", OS.CONFIRMED, False, PS.PENDING, DS.ASSIGNED, None)
    order, delivery = _seed(db, sample_user, delivery_driver, case)
    order_id, delivery_id = order.id, delivery.id
    cancelled = client.post(CANCEL_URL.format(order_id=order_id), headers=_headers(app, sample_user))
    assert cancelled.status_code == 200, cancelled.get_json()
    histories_before = (
        DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).count(),
        OrderStatusHistory.query.filter_by(order_id=order_id).count(),
    )
    stale = db.session.get(Order, order_id)
    assert (stale.status, delivery.status) == (OrderStatus.CANCELLED, DeliveryStatus.CANCELLED)
    set_committed_value(stale, "status", OrderStatus.CONFIRMED)
    set_committed_value(delivery, "status", DeliveryStatus.ASSIGNED)
    customer_told.clear()
    staff_told.forget()

    response = writer.send(client, headers, order_id, delivery_id)

    body = response.get_json()
    assert response.status_code == writer.status_code, body
    assert writer.observed(body) == writer.expected(order_id, delivery_id)
    db.session.expire_all()
    assert db.session.get(Order, order_id).status is OrderStatus.CANCELLED
    assert db.session.get(Delivery, delivery_id).status is DeliveryStatus.CANCELLED
    assert (
        DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).count(),
        OrderStatusHistory.query.filter_by(order_id=order_id).count(),
    ) == histories_before
    # No "being prepared", "on its way" or any other notice for an order the customer cancelled.
    assert customer_told == []
    assert staff_told.everything() == []


class StartOnCancelled(NamedTuple):
    """A caller of `DeliveryService.begin_delivery_in_transit`, and its refusal, for a delivery the
    DB really holds as ASSIGNED on an order that is CANCELLED."""

    name: str
    send: Callable  # (client, app, driver, admin_headers, delivery_id) -> response
    observed: Callable  # (response) -> what the refusal is pinned on
    expected: Callable  # (delivery_id) -> what it must equal


START_ON_CANCELLED = [
    StartOnCancelled(
        "admin-bulk-mark-in-transit",
        lambda client, _app, _driver, admin_headers, delivery_id: _bulk("mark_in_transit", "delivery")(
            client, admin_headers, None, delivery_id
        ),
        lambda response: (response.status_code, response.get_json()["data"]["results"]),
        lambda delivery_id: (
            200,
            _one_failed_item("delivery_id", "Cannot start delivery at the current stage")(None, delivery_id),
        ),
    ),
    StartOnCancelled(
        "legacy-driver-start-delivery",
        lambda client, app, driver, _admin_headers, delivery_id: client.post(
            LEGACY_START_DELIVERY_URL.format(delivery_id=delivery_id), headers=_headers(app, driver)
        ),
        # The test DB has no translation rows, so the route's copy comes back as its key.
        lambda response: (response.status_code, response.get_json()),
        lambda _delivery_id: (400, {"error": "api.delivery.error.cannot_start_at_stage"}),
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize("caller", START_ON_CANCELLED, ids=[caller.name for caller in START_ON_CANCELLED])
def test_a_start_on_an_accepted_stop_whose_order_is_cancelled_is_refused(
    app, client, db, sample_user, delivery_driver, admin_claim_headers, customer_told, staff_told, caller
):
    """`begin_delivery_in_transit` refuses unless the delivery is still ASSIGNED **and** its order
    is not CANCELLED, with one message for both. This case pins the order half: the DB really
    holds an ASSIGNED delivery on a CANCELLED order, as only legacy data can. The delivery
    re-read half is pinned by
    `test_a_start_whose_read_predates_the_deliverys_cancel_meets_the_row_as_it_is`. (The
    stale-writer case above rewinds both rows, so either half alone refuses it there.) Both callers
    meet the refusal as their own answer: the bulk action as a failed item, the legacy driver route
    as a 400 (not its 500 "start failed"). The delivery stays ASSIGNED and gains no history row."""
    case = Case(f"start-on-cancelled-{caller.name}", OS.CANCELLED, False, PS.PENDING, DS.ASSIGNED, None)
    _order, delivery = _seed(db, sample_user, delivery_driver, case)
    delivery_id = delivery.id
    history_before = DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).count()

    response = caller.send(client, app, delivery_driver, admin_claim_headers, delivery_id)

    assert caller.observed(response) == caller.expected(delivery_id)
    db.session.expire_all()
    assert db.session.get(Delivery, delivery_id).status is DeliveryStatus.ASSIGNED
    assert DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).count() == history_before
    assert customer_told == []
    assert staff_told.everything() == []


@pytest.mark.integration
def test_a_start_whose_read_predates_the_deliverys_cancel_meets_the_row_as_it_is(
    app, client, db, sample_user, delivery_driver, admin_claim_headers
):
    """The delivery re-read half of `begin_delivery_in_transit`'s guard. The order is CONFIRMED,
    so the order half cannot refuse; the delivery row is really `cancelled` in the DB (a leftover
    like order 153), and only the session's copy still says `assigned`, as a bulk request that read
    it earlier would. The locked re-read sees the row as it is and refuses; the row keeps its
    status and gains no history row."""
    case = Case("start-stale-delivery", OS.CONFIRMED, False, PS.PENDING, DS.CANCELLED, None)
    _order, delivery = _seed(db, sample_user, delivery_driver, case)
    delivery_id = delivery.id
    history_before = DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).count()
    assert delivery.status is DeliveryStatus.CANCELLED
    set_committed_value(delivery, "status", DeliveryStatus.ASSIGNED)

    response = _bulk("mark_in_transit", "delivery")(client, admin_claim_headers, None, delivery_id)

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["results"] == _one_failed_item(
        "delivery_id", "Cannot start delivery at the current stage"
    )(None, delivery_id)
    db.session.expire_all()
    assert db.session.get(Delivery, delivery_id).status is DeliveryStatus.CANCELLED
    assert DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).count() == history_before


@pytest.mark.integration
def test_a_refused_bulk_start_is_logged_as_a_refusal_not_a_crash(
    app, client, db, sample_user, delivery_driver, admin_claim_headers, caplog
):
    """The bulk delivery loop counts an expected refusal (a ValidationError) as a failed item and
    logs it at WARNING, never as an ERROR with a traceback. The app logger does not propagate to
    the root, so caplog's handler is attached to it directly."""
    case = Case("start-refused-logged", OS.CANCELLED, False, PS.PENDING, DS.ASSIGNED, None)
    _order, delivery = _seed(db, sample_user, delivery_driver, case)
    delivery_id = delivery.id
    caplog.set_level(logging.WARNING, logger=app.logger.name)
    app.logger.addHandler(caplog.handler)
    try:
        response = _bulk("mark_in_transit", "delivery")(client, admin_claim_headers, None, delivery_id)
    finally:
        app.logger.removeHandler(caplog.handler)

    assert response.get_json()["data"]["results"]["failed_count"] == 1
    refusals = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and f"refused for delivery_id={delivery_id}" in record.getMessage()
    ]
    assert len(refusals) == 1, [record.getMessage() for record in caplog.records]
    assert [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR and "Bulk delivery action" in record.getMessage()
    ] == []


@pytest.mark.integration
def test_a_legacy_start_refused_for_want_of_a_bottle_session_says_so(
    app, client, db, sample_user, sample_product, monkeypatch
):
    """The legacy driver route passes a bottle-session refusal through as its own answer (the
    completion route's shape: the message and its `error_code`), rather than "cannot be started at
    this stage". Strict enforcement on, an order with returnable bottles, and a driver with no open
    session: BOTTLE_SESSION_REQUIRED."""
    from tests.unit.test_bottle_session_integration import _make_delivery, _make_driver, _make_order_with_bottles

    monkeypatch.setitem(app.config, "BOTTLE_SESSION_ENFORCEMENT_STRICT", True)
    driver = _make_driver(db, "+998901241101", "NoSession")
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _make_delivery(db, order, driver)
    db.session.commit()
    order_id, delivery_id, driver_id = order.id, delivery.id, driver.id

    response = client.post(LEGACY_START_DELIVERY_URL.format(delivery_id=delivery_id), headers=_headers(app, driver))

    assert response.status_code == 400, response.get_json()
    assert response.get_json() == {
        "error": (
            f"Order {order_id} has no bottle-session binding and driver {driver_id} has no open bottle "
            "session; open a session to continue delivering this order."
        ),
        "error_code": "BOTTLE_SESSION_REQUIRED",
    }
    db.session.expire_all()
    assert db.session.get(Delivery, delivery_id).status is DeliveryStatus.ASSIGNED


@pytest.mark.integration
def test_bulk_process_refuses_a_cancelled_order_and_still_processes_the_next(
    app, client, db, sample_user, delivery_driver, admin_claim_headers
):
    """A refused item is recorded as failed, and the loop still reads the next item afresh and
    processes it; the refused order is untouched. SQLite takes no row locks, so the lock release
    itself is pinned by the Postgres test
    test_bulk_process_releases_a_refused_items_order_lock_before_the_next_item."""
    refused, _ = _seed(
        db, sample_user, delivery_driver, Case("bulk-refused", OS.CANCELLED, False, PS.PENDING, DS.CANCELLED, None)
    )
    ready, _ = _seed(
        db, sample_user, delivery_driver, Case("bulk-ready", OS.CONFIRMED, False, PS.PENDING, DS.SCHEDULED, None)
    )
    refused_id, ready_id = refused.id, ready.id

    # The four keys `adminService.performBulkAction` sends.
    response = client.post(
        BULK_ACTIONS_URL,
        json={"action": "process", "target_type": "order", "target_ids": [refused_id, ready_id], "parameters": {}},
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["results"] == {
        "success_count": 1,
        "failed_count": 1,
        "errors": [{"order_id": refused_id, "error": f"Cannot process order with status {OrderStatus.CANCELLED}"}],
        "total_errors": 1,
    }
    db.session.expire_all()
    assert (db.session.get(Order, refused_id).status, db.session.get(Order, ready_id).status) == (
        OrderStatus.CANCELLED,
        OrderStatus.PREPARING,
    )


@pytest.mark.integration
def test_another_customers_order_is_not_found_and_left_alone(
    app, client, db, sample_user, second_sample_user, delivery_driver, customer_told
):
    """Ownership is checked on the locked row: a foreign id answers exactly like a missing one."""
    order, _delivery = _seed(
        db, sample_user, delivery_driver, Case("foreign", OS.PENDING, False, PS.PENDING, None, None)
    )

    response = client.post(CANCEL_URL.format(order_id=order.id), headers=_headers(app, second_sample_user))

    assert response.status_code == 404, response.get_json()
    assert response.get_json()["success"] is False
    db.session.expire_all()
    assert db.session.get(Order, order.id).status is OrderStatus.PENDING
    assert customer_told == []


class TestTheWebRefusalCopyIsSeeded:
    def test_there_is_one_web_copy_per_published_reason_code(self):
        from business_app.services.order_service import (
            CUSTOMER_CANCEL_AWAITING_NEW_DATE,
            CUSTOMER_CANCEL_NOT_CANCELLABLE,
            CUSTOMER_CANCEL_PAID,
            CUSTOMER_CANCEL_WITH_DRIVER,
        )

        assert set(REFUSAL_COPY_KEY) == {
            CUSTOMER_CANCEL_NOT_CANCELLABLE,
            CUSTOMER_CANCEL_AWAITING_NEW_DATE,
            CUSTOMER_CANCEL_PAID,
            CUSTOMER_CANCEL_WITH_DRIVER,
        }

    def test_every_web_refusal_is_trilingual_points_to_the_bot_and_names_no_phone(self):
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        for key in REFUSAL_COPY_KEY.values():
            assert key in BACKEND_TRANSLATIONS, f"{key} would reach the customer as a raw key"
            assert set(BACKEND_TRANSLATIONS[key]) == {"en", "uz", "ru"}, key
            for language, value in BACKEND_TRANSLATIONS[key].items():
                assert "{bot_url}" in value, f"{key}/{language} does not point to the bot"
                assert not re.search(r"\d{5,}|tel:|\+998", value), f"{key}/{language} names a phone"
            assert "’" not in BACKEND_TRANSLATIONS[key]["uz"], f"{key}/uz has a curly apostrophe"

    def test_the_awaiting_copy_never_says_a_driver_has_the_order(self):
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        copy = BACKEND_TRANSLATIONS["api.orders.cancel_refused.awaiting_new_date"]
        assert "driver" not in copy["en"].lower()
        assert "haydovchi" not in copy["uz"].lower()
        assert "водител" not in copy["ru"].lower()


# The WITH_DRIVER refusal now means the driver has picked the order up (F5, amended 2026-10-21).
# The owner approved this wording; it is pinned verbatim, for the bot chat and for the web.
WITH_DRIVER_COPY = {
    "telegram.orders.cancel_refused.with_driver": {
        "uz": "Haydovchi buyurtmangizni allaqachon olib ketdi, u yo'lda. "
        "Uni o'zgartirish kerak bo'lsa, shu chatda bizga yozing.",
        "ru": "Водитель уже забрал ваш заказ, он в пути. Если нужно что-то изменить, напишите нам здесь, в этом чате.",
        "en": "The driver has already picked up your order and it's on its way. "
        "If you need to change it, write to us here in the chat.",
    },
    "api.orders.cancel_refused.with_driver": {
        "uz": "Haydovchi buyurtmangizni allaqachon olib ketdi, u yo'lda, shuning uchun uni bu yerda bekor qilib "
        "bo'lmaydi. Uni o'zgartirish kerak bo'lsa, Telegram botimizga yozing: {bot_url}",
        "ru": "Водитель уже забрал ваш заказ, он в пути, поэтому отменить его здесь нельзя. "
        "Если нужно что-то изменить, напишите нам в Telegram-бот: {bot_url}",
        "en": "The driver has already picked up your order and it's on its way, so it can't be cancelled here. "
        "If you need to change it, message us in our Telegram bot: {bot_url}",
    },
}


def test_the_with_driver_refusal_says_the_driver_has_picked_the_order_up():
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

    assert {key: BACKEND_TRANSLATIONS[key] for key in WITH_DRIVER_COPY} == WITH_DRIVER_COPY


# The bot's NOT_CANCELLABLE refusal gained a next step (owner-approved wording, 2026-10-22): like
# the other three, it now points to this chat. The web copy, which already points to the bot link,
# is unchanged. Pinned verbatim.
NOT_CANCELLABLE_BOT_COPY = {
    "telegram.orders.cancel_refused.not_cancellable": {
        "uz": "Bu buyurtmani endi bekor qilib bo'lmaydi. Savolingiz bo'lsa, shu chatda bizga yozing.",
        "ru": "Этот заказ больше нельзя отменить. Если у вас есть вопрос, напишите нам здесь, в этом чате.",
        "en": "This order can no longer be cancelled. If you have a question about it, write to us here in the chat.",
    },
}


def test_the_not_cancellable_refusal_in_the_bot_says_what_to_do_next():
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

    assert {key: BACKEND_TRANSLATIONS[key] for key in NOT_CANCELLABLE_BOT_COPY} == NOT_CANCELLABLE_BOT_COPY
