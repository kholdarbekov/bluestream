"""A reported failed attempt no longer re-dates the delivery behind dispatch's back (R19).

`reschedule_failed_delivery_task` was a second, divergent re-dater. It reset a FAILED row
to SCHEDULED for tomorrow and skipped:
  * the unassign SSOT and the history row;
  * the bottle unbind and the counter sync;
  * `Order.delivery_date`.
It then auto-assigned the row and sent a `delivery_rescheduled` notice that had no
template. Re-dating now has one path, `OrderScheduleService.reschedule` (the admin
Reschedule and both Re-dispatch buttons). So the task is gone, and so is its only
producer: the `failed_attempt` branch of `handle_delivery_exception_task`, fed by
`POST /api/v1/delivery/driver/report-issue/<id>`.

That endpoint's own failed-attempt write (no history, no unbind) is a known divergent path
and is out of scope (spec §10). This file pins only that it no longer re-dates anything.

Spec: docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md (R19).
"""

import inspect
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.tasks import delivery_tasks
from business_app.tasks.delivery_tasks import handle_delivery_exception_task
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod
from tests.unit.test_delivery_service_business_rules import _unshadow_task_publish

pytestmark = pytest.mark.integration

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


def test_the_legacy_redater_task_is_gone():
    assert not hasattr(delivery_tasks, "reschedule_failed_delivery_task")


def test_a_reported_failed_attempt_is_recorded_and_re_dates_nothing(
    app, client, db, delivery_driver, sample_user, enqueued
):
    delivery = _delivery_at_the_door(db, sample_user, delivery_driver)
    with app.app_context():
        token = create_access_token(identity=str(delivery_driver.id))

    response = client.post(
        f"/api/v1/delivery/driver/report-issue/{delivery.id}",
        json={"issue_type": "failed_attempt", "details": {"reason": REASON}},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert enqueued == [
        (handle_delivery_exception_task.name, (delivery.id, "failed_attempt", {"reason": REASON}), {})
    ]

    # Run what the endpoint enqueued, the way the worker would.
    _name, args, kwargs = enqueued.pop()
    result = handle_delivery_exception_task(*args, **kwargs)

    assert result == {"success": True, "exception_type": "failed_attempt", "delivery_id": delivery.id}
    # Before R19 this list held ("...reschedule_failed_delivery_task", (delivery.id,), {}).
    # That task re-dated the delivery to tomorrow and auto-assigned it without any
    # dispatcher being involved.
    assert enqueued == []
    db.session.expire_all()
    row = db.session.get(Delivery, delivery.id)
    assert (row.status, row.delivery_attempts, row.failed_delivery_reason) == (DeliveryStatus.FAILED, 1, REASON)
    assert row.delivery_person_id == delivery_driver.id
    assert db.session.get(Order, delivery.order_id).delivery_date is None
