"""Unit tests for staff notification tasks.

Regression: ``notify_staff_new_order`` must NOT build a second Flask app via
``create_app()`` on every order — Celery's ContextTask already runs each task
inside ``app.app_context()``. The redundant build re-ran env validation (and
its false SENDGRID warning) ~8x/day in prod. It must rely on the ambient
context like every sibling notify_* task.
"""

from datetime import UTC, datetime, time
from unittest.mock import MagicMock, patch

from business_app import db as _db
from business_app.models.delivery import DeliveryPerson
from business_app.models.order import Order
from business_app.models.user import User
from business_app.tasks.staff_tasks import notify_staff_new_order
from shared.enums import OrderStatus, UserRole, UserType


def _seed_active_driver(telegram_id="900900900"):
    user = User(
        email=f"driver_{telegram_id}@example.com",
        phone="+998900000009",
        password_hash="x",
        first_name="Drive",
        last_name="Er",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        telegram_id=telegram_id,
        status="active",
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    _db.session.add(user)
    _db.session.flush()
    dp = DeliveryPerson(
        user_id=user.id,
        full_name="Drive Er",
        phone="+998900000009",
        is_active=True,
        notifications_muted=False,
    )
    _db.session.add(dp)
    _db.session.commit()
    return user


def test_notify_staff_new_order_does_not_build_a_second_app(app, db):
    driver = _seed_active_driver()
    order_info = {"order_id": 4242, "order_number": "AD_004242_26"}

    with patch("business_app.create_app") as create_app_mock, patch(
        "business_app.tasks.staff_tasks._send_staff_webhook"
    ) as webhook_mock:
        webhook_mock.return_value = True
        notify_staff_new_order.run(order_id=4242, order_info=order_info)

    create_app_mock.assert_not_called()
    webhook_mock.assert_called_once()
    endpoint, payload = webhook_mock.call_args.args[0], webhook_mock.call_args.args[1]
    assert endpoint == "/internal/new-order"
    assert driver.telegram_id in payload["delivery_person_telegram_ids"]
    assert payload["order_info"] == order_info


def test_notify_staff_new_order_builds_order_info_with_delivery_window(app, db):
    """When no ``order_info`` is passed, the task builds one from the Order --
    which used to read the now-gone ``order.delivery_time_slot`` column and
    AttributeError on every push (silently swallowed into the task's retry,
    so no driver was ever notified of a new order via this path). It must
    build the structured ``delivery_window`` dict staff_bot renders locally
    (Task 13 converted the bot's last reader off the legacy ``time_slot``
    string, and Task 14 dropped that shim from this payload)."""
    driver = _seed_active_driver()
    order = Order(
        user_id=driver.id,
        status=OrderStatus.CONFIRMED,
        total_amount=1000,
        delivery_window_start=time(19, 0),
        order_source="web",
    )
    _db.session.add(order)
    _db.session.commit()
    order_id = order.id

    with patch("business_app.tasks.staff_tasks._send_staff_webhook") as webhook_mock:
        webhook_mock.return_value = True
        notify_staff_new_order.run(order_id=order_id, order_info=None)

    webhook_mock.assert_called_once()
    payload = webhook_mock.call_args.args[1]
    order_info = payload["order_info"]
    assert "time_slot" not in order_info
    assert order_info["delivery_window"] == {
        "start": "19:00",
        "end": None,
        "kind": "after",
        "label": "after 19:00",
    }
