"""A failed delivery alerts the staff who can give it a new date (F7).

F7 of docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md. The
admin panel has no push channel, so the staff bot is the only one that reaches a person.
`StaffService.update_delivery_status` queues `notify_delivery_failed` once a failure
commits, from the driver bot and from the admin Delivery page alike, whatever the reason
(F3). The failure leaves the order's status alone (F1). The enqueue has its own guard:
`failed` is terminal, so a broker outage that turned the committed failure into a 500
would leave the driver a re-tap the backend only refuses.

The fan-out re-reads the delivery and picks the recipients: operators the staff bot would
let in, and active admins and managers, each with a Telegram id. A person who holds both
roles gets one message, as an operator. The person who marked the failure gets none. The
card names the same customer and driver as the operator's failed-list card, because both
come from one builder. Each recipient gets one `push_delivery_failed`, which re-checks on
every run, retries included. Its event id carries the attempt count, so a delivery that
fails, is re-dated and fails again alerts twice, and a retry left over from the first
failure sends nothing (Review Focus 4 of
docs/superpowers/plans/2026-09-25-failed-delivery-awaits-new-date.md).

Every step a person takes goes through the route their client calls: the driver's
`PUT /api/v1/staff/delivery/<id>/status` with the staff bot's body, the admin Delivery
page's `PUT /api/v1/admin/deliveries/<id>`, the operator's
`GET /api/v1/staff/delivery/failed/<id>` and `POST /api/v1/staff/delivery/redispatch/<id>`,
and the driver's accept. The tasks run as the worker runs them: `.run` with the recorded
arguments, after a fresh read. Faked: Celery publishing and retrying, and
`_send_staff_webhook`, the HTTP call to the staff bot.
"""

import inspect
from datetime import time

import pytest
from celery.exceptions import Retry
from kombu.exceptions import OperationalError

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryStatusHistory
from business_app.models.order import Order
from business_app.models.staff import StaffActivityLog
from business_app.models.user import User
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.tasks import notification_tasks, staff_tasks
from business_app.utils.password_security import hash_password
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, UserRole, UserStatus, UserType
from shared.staff_constants import FAILED_DELIVERY_REASONS, STAFF_ACTIONS
from tests.integration.test_redispatch_reschedules_to_today import (  # noqa: F401 -- enqueued is a fixture
    _freeze_business_clock,
    enqueued,
)
from tests.integration.test_staff_delivery_ownership_guard import TOTAL, _bearer, _delivery, _driver
from tests.unit.test_delivery_rescheduled_notification import live_send  # noqa: F401 -- a fixture
from tests.unit.test_delivery_service_business_rules import _patch_task_publish, _task_spy

pytestmark = pytest.mark.integration

STATUS = "/api/v1/staff/delivery/{}/status"
ACCEPT = "/api/v1/staff/delivery/accept/{}"
REDISPATCH = "/api/v1/staff/delivery/redispatch/{}"
FAILED_ROW = "/api/v1/staff/delivery/failed/{}"
ADMIN_DELIVERY = "/api/v1/admin/deliveries/{}"
ADMIN_ORDER_STATUS = "/api/v1/admin/orders/{}/status"
WEBHOOK = "/internal/delivery-failed"
# After the drivers' 09:00 shift start (the `working_hours_start` column default), so a
# re-date to today lands straight in the pool instead of being held (R5).
MIDDAY = time(12, 0)
# Who and where: what the alert and the operator's failed-list card must agree on.
CARD_KEYS = ("delivery_id", "order_id", "order_number", "customer_name", "customer_phone", "address", "driver_name")


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def _stop(db, customer, driver, *, status, number):
    """`_delivery`, after an earlier cancelled order of the same customer.

    The tables are fresh for every test, so without it the stop would be order 1 and
    delivery 1, and an alert that swapped the two ids would still match.
    """
    db.session.add(
        Order(
            user_id=customer.id,
            order_number=f"{number}-EARLIER",
            status=OrderStatus.CANCELLED,
            subtotal=TOTAL,
            total_amount=TOTAL,
            payment_method=PaymentMethod.CASH,
        )
    )
    db.session.commit()
    delivery = _delivery(db, customer, driver, status=status, number=number)
    assert delivery.id != delivery.order_id
    return delivery


def _staff(db, *, phone, first_name, role, telegram_id, language="en", staff_roles=None, status=UserStatus.ACTIVE):
    user = User(
        phone=phone,
        password_hash=hash_password("StaffPassword123!"),
        first_name=first_name,
        last_name="Staff",
        user_type=UserType.STAFF,
        role=role,
        status=status,
        telegram_id=telegram_id,
        preferred_language=language,
        staff_roles=staff_roles or [],
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _driver_fails(app, client, driver, delivery_id, reason):
    """The staff bot's failure tap: `api_client.update_delivery_status` sends this body."""
    response = client.put(
        STATUS.format(delivery_id),
        json={"status": "failed", "metadata": {"fail_reason": reason}},
        headers=_bearer(app, driver),
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["status"] == "failed"


def _worker_runs(db, task, call):
    """Run one recorded publish as the worker would: its own arguments, a fresh read."""
    args, kwargs = call
    db.session.expire_all()
    return task.run(*args, **kwargs)


def _webhook(monkeypatch, *answers):
    """`_send_staff_webhook`, faked. Each call is bound against the real signature and
    recorded as `(endpoint, body)`, and answered with the next of `answers` (True: the
    staff bot took it). A send the test did not script fails the test."""
    signature = inspect.signature(staff_tasks._send_staff_webhook)
    answers = list(answers)
    sent = []

    def fake(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        sent.append((bound.arguments["endpoint"], dict(bound.arguments["data"])))
        return answers.pop(0)

    monkeypatch.setattr(staff_tasks, "_send_staff_webhook", fake)
    return sent


def _retries(monkeypatch):
    """`push_delivery_failed.retry`, faked: records the call and raises `Retry` as Celery
    does. Patched in the instance dict, the way `_patch_task_publish` patches `delay`, so
    nothing outlives the test."""
    task = staff_tasks.push_delivery_failed
    signature = inspect.signature(task.retry)
    calls = []

    def fake(*args, **kwargs):
        signature.bind(*args, **kwargs)
        calls.append(kwargs)
        raise Retry()

    monkeypatch.setitem(vars(task), "retry", fake)
    return calls


# --------------------------------------------------------------------------- #
# 1. The trigger
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("reason", FAILED_DELIVERY_REASONS)
def test_every_failure_reason_keeps_the_order_and_queues_one_alert_carrying_its_code(
    app, client, db, operator_user, sample_user, enqueued, live_send, reason
):
    """F3: all five reasons behave the same (spec §8, backend item 2). A `picked_up` queues
    no alert. The failure leaves the order's status where `picked_up` put it (F1), so the
    order awaits a new date, and it queues exactly one alert, whose card carries the code.

    F17: the customer hears nothing. They are reachable in the bot and by email, so only
    the milestone gate keeps them silent: the failure queues the customer's usual status
    update, and that update, run on the real send path, sends nothing anywhere.

    On this route the JWT identity reaches the service as a str. The task's contract is
    the users.id int, and the fan-out compares it with `User.id`."""
    operator_user.telegram_id = "700000401"
    sample_user.telegram_id = "998900001299"
    sample_user.is_bot_active = True
    db.session.commit()
    assert sample_user.email
    driver = _driver(db, phone="+998901310011", first_name="Jahongir")
    delivery = _stop(db, sample_user, driver, status=DeliveryStatus.ASSIGNED, number="ORD-F7-DOOR")
    delivery_id, order_id = delivery.id, delivery.order_id

    picked_up = client.put(STATUS.format(delivery_id), json={"status": "picked_up"}, headers=_bearer(app, driver))
    assert picked_up.status_code == 200, picked_up.get_data(as_text=True)
    assert staff_tasks.notify_delivery_failed.name not in [name for name, _args, _kwargs in enqueued]
    db.session.expire_all()
    assert db.session.get(Order, order_id).status == OrderStatus.OUT_FOR_DELIVERY
    enqueued.clear()

    _driver_fails(app, client, driver, delivery_id, reason)

    failed = DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id, new_status=DeliveryStatus.FAILED).one()
    # Everything the failure publishes: the customer's status update and the staff alert.
    assert sorted(enqueued) == sorted(
        [
            (notification_tasks.send_delivery_update_task.name, (failed.id,), {}),
            (staff_tasks.notify_delivery_failed.name, (delivery_id, driver.id), {}),
        ]
    )
    db.session.expire_all()
    order = db.session.get(Order, order_id)
    row = db.session.get(Delivery, delivery_id)
    assert (order.status, row.status, row.failed_delivery_reason) == (
        OrderStatus.OUT_FOR_DELIVERY,
        DeliveryStatus.FAILED,
        reason,
    )
    assert OrderScheduleService.awaiting_new_date(order) is True

    # The customer's update, as the worker runs it: no Telegram message and no email.
    assert _worker_runs(db, notification_tasks.send_delivery_update_task, ((failed.id,), {})) == {}
    live_send.assert_not_called()

    enqueued.clear()
    _worker_runs(db, staff_tasks.notify_delivery_failed, ((delivery_id, driver.id), {}))

    [(name, push, _kwargs)] = enqueued
    assert name == staff_tasks.push_delivery_failed.name
    assert (push[:3], push[3]["reason"]) == ((delivery_id, 1, 700000401), reason)


def test_a_broker_outage_leaves_the_failure_committed_and_the_customer_update_queued(
    app, client, db, sample_user, monkeypatch
):
    """The alert is best-effort and sits in its own guard. The failure before it is
    committed, and the rest of the method after it still runs."""
    driver = _driver(db, phone="+998901310021", first_name="Sanjar")
    delivery = _stop(db, sample_user, driver, status=DeliveryStatus.ARRIVED, number="ORD-F7-BROKER")
    delivery_id = delivery.id
    customer_updates = _task_spy(monkeypatch, notification_tasks.send_delivery_update_task, "delay")
    refused_publishes = []
    signature = inspect.signature(staff_tasks.notify_delivery_failed.run)

    def broker_down(*args, **kwargs):
        signature.bind(*args, **kwargs)
        refused_publishes.append((args, kwargs))
        raise OperationalError("[Errno 111] Connection refused")

    _patch_task_publish(monkeypatch, staff_tasks.notify_delivery_failed, "delay", broker_down)

    _driver_fails(app, client, driver, delivery_id, "customer_refused")

    assert refused_publishes == [((delivery_id, driver.id), {})]
    db.session.expire_all()
    row = db.session.get(Delivery, delivery_id)
    assert (row.status, row.failed_delivery_reason, row.delivery_attempts) == (
        DeliveryStatus.FAILED,
        "customer_refused",
        1,
    )
    failed = DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id, new_status=DeliveryStatus.FAILED).one()
    assert customer_updates == [((failed.id,), {})]
    logged = StaffActivityLog.query.filter_by(
        entity_type="delivery", entity_id=delivery_id, action=STAFF_ACTIONS["DELIVERY_STATUS_UPDATED"]
    ).one()
    assert logged.metadata_["new_status"] == "failed"


# --------------------------------------------------------------------------- #
# 2. The fan-out
# --------------------------------------------------------------------------- #


def test_each_operator_admin_and_manager_gets_one_alert_and_the_admin_who_marked_it_none(
    app, client, db, admin_claim_headers, admin_user, operator_user, sample_user, monkeypatch
):
    """The admin Delivery page marks the failure, so the actor is an admin and the driver
    is someone else. The card names the delivery's own driver, never the actor."""
    driver = _driver(db, phone="+998901310001", first_name="Farhod")
    driver.telegram_id = "700000009"  # a driver is never a recipient
    admin_user.telegram_id = "700000002"  # the actor
    operator_user.telegram_id = "700000001"
    operator_user.preferred_language = "ru"
    sample_user.telegram_id = "700000010"  # the customer
    db.session.commit()
    # Both roles at once: one message, with the operator's buttons.
    manager_operator = _staff(
        db,
        phone="+998901310002",
        first_name="Malika",
        role=UserRole.MANAGER,
        telegram_id="700000003",
        staff_roles=["operator"],
    )
    # No language on file, so the card goes out in Uzbek. It is cleared after the insert,
    # because the ORM leaves a None out of the INSERT and the column's "en" default fills it.
    manager_operator.preferred_language = None
    _staff(db, phone="+998901310003", first_name="Bobur", role=UserRole.ADMIN, telegram_id="700000004")
    # A manager who is not an operator: only the admins-and-managers half reaches her.
    _staff(
        db,
        phone="+998901310008",
        first_name="Aziza",
        role=UserRole.MANAGER,
        telegram_id="700000007",
        language="ru",
    )
    # Never recipients: the staff bot refuses a deactivated staff profile, an inactive
    # account gets nothing, and a person with no Telegram id cannot be reached.
    deactivated = _staff(
        db,
        phone="+998901310004",
        first_name="Dilshod",
        role=UserRole.OPERATOR,
        telegram_id="700000005",
        staff_roles=["delivery_driver"],
    )
    db.session.add(
        DeliveryPerson(user_id=deactivated.id, full_name="Dilshod Staff", phone=deactivated.phone, is_active=False)
    )
    _staff(
        db,
        phone="+998901310005",
        first_name="Kamola",
        role=UserRole.OPERATOR,
        telegram_id="700000006",
        status=UserStatus.INACTIVE,
    )
    _staff(db, phone="+998901310006", first_name="Nodir", role=UserRole.OPERATOR, telegram_id=None)
    _staff(
        db,
        phone="+998901310007",
        first_name="Zarina",
        role=UserRole.MANAGER,
        telegram_id="700000008",
        status=UserStatus.INACTIVE,
    )
    delivery = _stop(db, sample_user, driver, status=DeliveryStatus.IN_TRANSIT, number="ORD-F7-ADMIN")
    delivery_id, order_id = delivery.id, delivery.order_id
    alerts = _task_spy(monkeypatch, staff_tasks.notify_delivery_failed, "delay")
    pushes = _task_spy(monkeypatch, staff_tasks.push_delivery_failed, "delay")

    # The Delivery page's body when it records a failure (`Delivery.js` handleUpdateSubmit).
    response = client.put(
        ADMIN_DELIVERY.format(delivery_id),
        json={"status": "failed", "notes": "Gate locked, nobody answered", "fail_reason": "wrong_address"},
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert alerts == [((delivery_id, admin_user.id), {})]

    _worker_runs(db, staff_tasks.notify_delivery_failed, alerts[0])

    alert = {
        "delivery_id": delivery_id,
        "order_id": order_id,
        "order_number": "ORD-F7-ADMIN",
        "reason": "wrong_address",
        "driver_name": "Farhod Driver",
        "attempts": 1,
        "customer_name": "Test User",
        "customer_phone": "+998901234567",
        "address": "ORD-F7-ADMIN Amir Temur 1, Tashkent",
    }

    def to(telegram_id, *, is_operator, language):
        payload = {
            **alert,
            "event_id": f"delivery-failed:{delivery_id}:1:{telegram_id}",
            "telegram_id": telegram_id,
            "is_operator": is_operator,
            "language": language,
        }
        return ((delivery_id, 1, telegram_id, payload), {})

    assert sorted(pushes, key=lambda call: call[0][2]) == [
        to(700000001, is_operator=True, language="ru"),
        to(700000003, is_operator=True, language="uz"),
        to(700000004, is_operator=False, language="en"),
        to(700000007, is_operator=False, language="ru"),
    ]


def test_one_recipient_the_alert_cannot_reach_costs_only_their_own_alert(app, client, db, sample_user, monkeypatch):
    """The fan-out is one loop over everyone the alert is for. A row whose Telegram id
    holds no number, or a publish the broker drops, is that one person's loss: the rest
    are still alerted, and the task still finishes."""
    _staff(db, phone="+998901310071", first_name="Otabek", role=UserRole.OPERATOR, telegram_id="@otabek")
    _staff(db, phone="+998901310072", first_name="Laylo", role=UserRole.OPERATOR, telegram_id="700000702")
    _staff(db, phone="+998901310073", first_name="Sardor", role=UserRole.OPERATOR, telegram_id="700000703")
    _staff(db, phone="+998901310074", first_name="Nigora", role=UserRole.ADMIN, telegram_id="700000704")
    driver = _driver(db, phone="+998901310075", first_name="Behzod")
    delivery = _stop(db, sample_user, driver, status=DeliveryStatus.ARRIVED, number="ORD-F7-ISOLATED")
    delivery_id = delivery.id
    alerts = _task_spy(monkeypatch, staff_tasks.notify_delivery_failed, "delay")
    signature = inspect.signature(staff_tasks.push_delivery_failed.run)
    pushes = []

    def broker_drops_laylo(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        if bound.arguments["telegram_id"] == 700000702:
            raise OperationalError("[Errno 104] Connection reset by peer")
        pushes.append(bound.arguments["telegram_id"])

    _patch_task_publish(monkeypatch, staff_tasks.push_delivery_failed, "delay", broker_drops_laylo)
    _driver_fails(app, client, driver, delivery_id, "customer_unavailable")

    result = _worker_runs(db, staff_tasks.notify_delivery_failed, alerts[0])

    assert pushes == [700000703, 700000704]
    assert result == {"success": True, "delivery_id": delivery_id, "recipients": 2}


def test_the_alert_names_the_same_customer_and_driver_as_the_operators_failed_card(
    app, client, db, operator_user, sample_user, monkeypatch
):
    """An operator meets one failed delivery twice: as the alert, and as its card from the
    failed list. Both come from `StaffService.failed_delivery_card`, so they name the same
    people. This customer has only a surname on file, which is where two separate
    builders parted ways ("None User" against "User")."""
    _freeze_business_clock(monkeypatch, MIDDAY)  # the one-row read draws date bounds from it
    operator_user.telegram_id = "700000501"
    sample_user.first_name = None
    db.session.commit()
    driver = _driver(db, phone="+998901310061", first_name="Sherzod")
    delivery = _stop(db, sample_user, driver, status=DeliveryStatus.ARRIVED, number="ORD-F7-SAME")
    delivery_id = delivery.id
    alerts = _task_spy(monkeypatch, staff_tasks.notify_delivery_failed, "delay")
    pushes = _task_spy(monkeypatch, staff_tasks.push_delivery_failed, "delay")
    _driver_fails(app, client, driver, delivery_id, "wrong_address")
    _worker_runs(db, staff_tasks.notify_delivery_failed, alerts[0])
    [push] = pushes
    payload = push[0][3]

    response = client.get(FAILED_ROW.format(delivery_id), headers=_bearer(app, operator_user))

    assert response.status_code == 200, response.get_data(as_text=True)
    row = response.get_json()["data"]["delivery"]
    assert {key: payload[key] for key in CARD_KEYS} == {key: row[key] for key in CARD_KEYS}
    assert (payload["customer_name"], payload["driver_name"]) == ("User", "Sherzod Driver")
    assert (payload["reason"], payload["attempts"]) == (row["failed_delivery_reason"], row["delivery_attempts"])


@pytest.mark.parametrize("meanwhile", ["redated", "order_cancelled"])
def test_the_fan_out_sends_nothing_once_the_delivery_no_longer_awaits_a_new_date(
    app, client, db, admin_claim_headers, operator_user, sample_user, monkeypatch, meanwhile
):
    """The alert waits in the queue. Before the worker reaches it, an operator may re-date
    the delivery, or an admin may cancel the order. A card asking for a date that was
    already given, or for an order that is gone, only sends an operator to a refusal."""
    _freeze_business_clock(monkeypatch, MIDDAY)
    operator_user.telegram_id = "700000201"
    db.session.commit()
    driver = _driver(db, phone="+998901310031", first_name="Umid")
    delivery = _stop(db, sample_user, driver, status=DeliveryStatus.ARRIVED, number=f"ORD-F7-{meanwhile}")
    delivery_id, order_id = delivery.id, delivery.order_id
    alerts = _task_spy(monkeypatch, staff_tasks.notify_delivery_failed, "delay")
    pushes = _task_spy(monkeypatch, staff_tasks.push_delivery_failed, "delay")
    _driver_fails(app, client, driver, delivery_id, "customer_unavailable")

    if meanwhile == "redated":
        response = client.post(REDISPATCH.format(delivery_id), headers=_bearer(app, operator_user))
    else:
        response = client.put(
            ADMIN_ORDER_STATUS.format(order_id),
            json={"status": "cancelled", "reason": "Customer moved to Samarkand"},
            headers=admin_claim_headers,
        )
    assert response.status_code == 200, response.get_data(as_text=True)
    [alert] = alerts

    _worker_runs(db, staff_tasks.notify_delivery_failed, alert)

    assert pushes == []


# --------------------------------------------------------------------------- #
# 3. The push
# --------------------------------------------------------------------------- #


def test_a_send_the_staff_bot_did_not_take_is_retried_under_the_same_event_id(
    app, client, db, operator_user, sample_user, monkeypatch
):
    """`_send_staff_webhook` answers False when the staff bot is down, restarting, or has no
    route yet. The alert is retried like `sales.push_sales_event`, and Celery replays the
    same arguments, so the staff bot's dedup sees one event id however many sends land."""
    operator_user.telegram_id = "700000301"
    db.session.commit()
    driver = _driver(db, phone="+998901310041", first_name="Olim")
    delivery = _stop(db, sample_user, driver, status=DeliveryStatus.ARRIVED, number="ORD-F7-RETRY")
    delivery_id = delivery.id
    alerts = _task_spy(monkeypatch, staff_tasks.notify_delivery_failed, "delay")
    pushes = _task_spy(monkeypatch, staff_tasks.push_delivery_failed, "delay")
    retries = _retries(monkeypatch)
    sent = _webhook(monkeypatch, False, True)
    _driver_fails(app, client, driver, delivery_id, "product_damaged")
    _worker_runs(db, staff_tasks.notify_delivery_failed, alerts[0])
    [push] = pushes

    with pytest.raises(Retry):
        _worker_runs(db, staff_tasks.push_delivery_failed, push)
    _worker_runs(db, staff_tasks.push_delivery_failed, push)  # Celery's retry: the same arguments

    payload = push[0][3]
    assert payload["event_id"] == f"delivery-failed:{delivery_id}:1:700000301"
    assert sent == [(WEBHOOK, payload), (WEBHOOK, payload)]
    assert [type(call["exc"]) for call in retries] == [RuntimeError]
    task = staff_tasks.push_delivery_failed
    assert (task.max_retries, task.default_retry_delay) == (3, 60)


def test_a_second_failure_alerts_again_and_the_first_failures_retry_sends_nothing(
    app, client, db, operator_user, sample_user, monkeypatch
):
    """Review Focus 4. The delivery fails, and the staff bot is down for the first alert.
    An operator re-dates the delivery, a driver takes it again, and it fails again.

    The first alert's retry comes round twice: after the re-date, and after the second
    failure. Both times it sends nothing and asks for no further retry. After the re-date
    the delivery no longer awaits a date. After the second failure it does again, but the
    attempt count has moved on, and the second failure queued an alert of its own. That
    one goes out under its own event id, so the staff bot's dedup does not swallow it.
    """
    _freeze_business_clock(monkeypatch, MIDDAY)
    operator_user.telegram_id = "700000101"
    operator_user.preferred_language = "uz"
    db.session.commit()
    driver = _driver(db, phone="+998901310051", first_name="Rustam")
    delivery = _stop(db, sample_user, driver, status=DeliveryStatus.ASSIGNED, number="ORD-F7-TWICE")
    delivery_id = delivery.id
    alerts = _task_spy(monkeypatch, staff_tasks.notify_delivery_failed, "delay")
    pushes = _task_spy(monkeypatch, staff_tasks.push_delivery_failed, "delay")
    retries = _retries(monkeypatch)
    # The staff bot does not take the first alert's first send. It takes the second alert.
    sent = _webhook(monkeypatch, False, True)

    # 1. The first failure. The alert is not taken, so Celery will retry it.
    _driver_fails(app, client, driver, delivery_id, "customer_unavailable")
    _worker_runs(db, staff_tasks.notify_delivery_failed, alerts[0])
    [first] = pushes
    assert first[0][:3] == (delivery_id, 1, 700000101)
    with pytest.raises(Retry):
        _worker_runs(db, staff_tasks.push_delivery_failed, first)

    # 2. An operator re-dates it to today. The retry comes round with nothing to ask for.
    redated = client.post(REDISPATCH.format(delivery_id), headers=_bearer(app, operator_user))
    assert redated.status_code == 200, redated.get_data(as_text=True)
    assert redated.get_json()["data"]["status"] == "scheduled"
    _worker_runs(db, staff_tasks.push_delivery_failed, first)

    # 3. The driver takes it again, and it fails again.
    accepted = client.post(ACCEPT.format(delivery_id), headers=_bearer(app, driver))
    assert accepted.status_code == 200, accepted.get_data(as_text=True)
    _driver_fails(app, client, driver, delivery_id, "customer_refused")
    db.session.expire_all()
    assert db.session.get(Delivery, delivery_id).delivery_attempts == 2
    assert alerts == [((delivery_id, driver.id), {}), ((delivery_id, driver.id), {})]
    _worker_runs(db, staff_tasks.notify_delivery_failed, alerts[1])
    assert len(pushes) == 2
    second = pushes[1]
    assert second[0][:3] == (delivery_id, 2, 700000101)
    assert {key: second[0][3][key] for key in ("event_id", "attempts", "reason")} == {
        "event_id": f"delivery-failed:{delivery_id}:2:700000101",
        "attempts": 2,
        "reason": "customer_refused",
    }

    # 4. The first alert's retry comes round again. The delivery awaits a date once more,
    #    but for attempt 2, so the retry still sends nothing. The second alert goes out.
    _worker_runs(db, staff_tasks.push_delivery_failed, first)
    _worker_runs(db, staff_tasks.push_delivery_failed, second)

    assert first[0][3]["event_id"] == f"delivery-failed:{delivery_id}:1:700000101"
    assert sent == [(WEBHOOK, first[0][3]), (WEBHOOK, second[0][3])]
    assert len(retries) == 1
