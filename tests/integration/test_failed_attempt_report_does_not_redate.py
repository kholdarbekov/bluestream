"""A driver can no longer report a failed attempt through the legacy issue route (F2).

`POST /api/v1/delivery/driver/report-issue/<id>` accepted `failed_attempt` and fed it to
the `failed_attempt` branch of `handle_delivery_exception_task`. That branch was a second
writer of `failed`, and it skipped what the real one does:
  * the DeliveryStatusHistory row;
  * the bottle-session unbind.
It also sent the customer `delivery_failed_attempt`, although nothing may reach the
customer at failure (F17). No client sends `failed_attempt`. A failed delivery is now
recorded only by `StaffService.update_delivery_status`, and it leaves `failed` only
through a reschedule or by cancelling its order.

The route stays, and still serves delay, vehicle_breakdown, customer_issue and
address_issue.

The branch once published `reschedule_failed_delivery_task`, a second re-dater deleted by
R19 of the 2026-09-23 reschedule spec. This file still pins that it is gone.

Specs: docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md (§3.2,
F2, F17); docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md (R19).
"""

import inspect
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.order import Order
from business_app.services.notification_service import NotificationService
from business_app.tasks import delivery_tasks
from business_app.tasks.delivery_tasks import handle_delivery_exception_task
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod
from tests.unit.test_delivery_service_business_rules import _unshadow_task_publish

pytestmark = pytest.mark.integration

REPORT = "/api/v1/delivery/driver/report-issue/{id}"
REASON = "Customer not available"


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


def _delivery_at_the_door(db, customer, driver):
    order = Order(
        user_id=customer.id,
        order_number="ORD-REPORT-1",
        status=OrderStatus.OUT_FOR_DELIVERY,
        subtotal=Decimal("30000.00"),
        delivery_fee=Decimal("0.00"),
        total_amount=Decimal("30000.00"),
        payment_method=PaymentMethod.CASH,
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver.id,
        status=DeliveryStatus.ARRIVED,
        scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _driver_headers(app, driver):
    with app.app_context():
        token = create_access_token(identity=str(driver.id))
    return {"Authorization": f"Bearer {token}"}


def test_the_legacy_redater_task_is_gone():
    assert not hasattr(delivery_tasks, "reschedule_failed_delivery_task")


def test_a_failed_attempt_report_is_refused_and_publishes_nothing(
    app, client, db, delivery_driver, sample_user, enqueued
):
    delivery = _delivery_at_the_door(db, sample_user, delivery_driver)
    delivery_id, order_id = delivery.id, delivery.order_id
    headers = _driver_headers(app, delivery_driver)

    refused = client.post(
        REPORT.format(id=delivery_id),
        json={"issue_type": "failed_attempt", "details": {"reason": REASON}},
        headers=headers,
    )
    unknown = client.post(
        REPORT.format(id=delivery_id), json={"issue_type": "teleported", "details": {}}, headers=headers
    )

    assert refused.status_code == 400, refused.get_data(as_text=True)
    # The route's own invalid-issue-type refusal: the body an unknown type gets, word for word.
    assert unknown.status_code == 400, unknown.get_data(as_text=True)
    assert refused.get_json() == unknown.get_json()
    assert set(refused.get_json()) == {"error"}
    assert enqueued == []

    db.session.expire_all()
    row = db.session.get(Delivery, delivery_id)
    assert (row.status, row.delivery_attempts, row.failed_delivery_reason, row.delivery_person_id) == (
        DeliveryStatus.ARRIVED, 0, None, delivery_driver.id,
    )
    assert DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).count() == 0
    assert db.session.get(Order, order_id).status == OrderStatus.OUT_FOR_DELIVERY


@pytest.mark.parametrize("issue_type", ["delay", "vehicle_breakdown", "customer_issue", "address_issue"])
def test_the_issue_types_the_route_still_serves_are_accepted_and_queued(
    app, client, db, delivery_driver, sample_user, enqueued, issue_type
):
    delivery = _delivery_at_the_door(db, sample_user, delivery_driver)
    delivery_id = delivery.id
    details = {"note": f"{issue_type} at gate 3"}

    response = client.post(
        REPORT.format(id=delivery_id),
        json={"issue_type": issue_type, "details": details},
        headers=_driver_headers(app, delivery_driver),
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert (body["issue_type"], body["delivery_id"]) == (issue_type, delivery_id)
    assert enqueued == [(handle_delivery_exception_task.name, (delivery_id, issue_type, details), {})]


def test_a_failed_attempt_queued_before_the_deploy_writes_nothing_and_tells_no_one(
    app, db, delivery_driver, sample_user, enqueued
):
    """A message the old route published is picked up by a worker that already runs the
    new task body. With the branch gone it falls through: nothing marks the delivery
    FAILED behind the driver's back, and the customer gets no `delivery_failed_attempt`
    (F17). `send_notification` is the customer-message boundary; the autospec keeps its
    signature."""
    delivery = _delivery_at_the_door(db, sample_user, delivery_driver)
    delivery_id = delivery.id

    with patch.object(NotificationService, "send_notification", autospec=True) as sent:
        result = handle_delivery_exception_task(delivery_id, "failed_attempt", {"reason": REASON})

    assert result == {"success": True, "exception_type": "failed_attempt", "delivery_id": delivery_id}
    sent.assert_not_called()
    assert enqueued == []
    db.session.expire_all()
    row = db.session.get(Delivery, delivery_id)
    assert (row.status, row.delivery_attempts, row.failed_delivery_reason) == (DeliveryStatus.ARRIVED, 0, None)
    assert DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).count() == 0
