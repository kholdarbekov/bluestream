"""The backend decides the delivery summary's Order again (spec 2026-10-21 §3, R2, R8).
`reorder_order_id` on the /internal/delivery-completed payload is the order's id when it is a
one-off order with at least one reorderable line, and null otherwise.

Driven through the Celery task every delivered path enqueues: `send_delivery_update_task.run`
for the DELIVERED delivery-history row (the repo idiom for a bound task, as in
test_delivery_rescheduled_notification.py). Only the HTTP hop to the bot is faked, with
`trigger_bot_webhook`'s own signature (create_autospec). The idempotency claim goes to a
FakeRedis, so no test writes the Redis DBs that test runs share.
"""
from datetime import UTC, datetime
from unittest.mock import create_autospec

import pytest

from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.services import notification_service
from business_app.tasks.notification_tasks import send_delivery_update_task
from business_app.utils.bot_webhook import trigger_bot_webhook
from shared.enums import DeliveryStatus, SubscriptionStatus
from tests.integration.reorder_reminder_builders import (
    FakeRedis,
    add_order_lines,
    at,
    delivered_order,
    make_customer,
    make_product,
    subscription,
    subscription_delivery,
)

pytestmark = pytest.mark.integration

ROUTE = "/internal/delivery-completed"


@pytest.fixture
def bot_post(monkeypatch):
    fake = create_autospec(trigger_bot_webhook, return_value={"success": True, "message": "Notification sent"})
    monkeypatch.setattr(notification_service, "trigger_bot_webhook", fake)
    monkeypatch.setattr(notification_service, "redis_client", FakeRedis())
    return fake


@pytest.fixture
def alice(db):
    return make_customer(db, telegram=True)


@pytest.fixture
def water(db, sample_category):
    return make_product(db, sample_category, "Water 19L")


def _dispatch(db, order):
    """Commit the DELIVERED delivery-history row the task is enqueued with, then run the task."""
    delivery = Delivery(order_id=order.id, status=DeliveryStatus.DELIVERED,
                        scheduled_date=datetime.now(UTC), scheduled_time_slot="09:00-12:00")
    db.session.add(delivery)
    db.session.flush()
    history = DeliveryStatusHistory(delivery_id=delivery.id, old_status=DeliveryStatus.ARRIVED,
                                    new_status=DeliveryStatus.DELIVERED, changed_at=datetime.now(UTC))
    db.session.add(history)
    db.session.commit()
    return send_delivery_update_task.run(history.id)


def _payload(order, user, reorder_order_id):
    # Non-returnable products and no ledger rows, so every bottle figure is "0".
    return {"order_id": order.id, "order_number": order.order_number, "telegram_id": int(user.telegram_id),
            "bottles_delivered": "0", "bottles_collected": "0", "balance": "0",
            "reorder_order_id": reorder_order_id}


def test_a_one_off_delivery_offers_order_again_on_itself(db, bot_post, alice, water):
    order = add_order_lines(db, delivered_order(db, alice, at(-1)), [(water, 5)], reward_lines=[(water, 1)])

    assert _dispatch(db, order) == {"success": True, "dispatched": True, "order_id": order.id}
    bot_post.assert_called_once_with(ROUTE, _payload(order, alice, order.id))


def test_a_subscription_delivery_offers_no_order_again(db, bot_post, alice, water):
    plan = subscription(db, alice, status=SubscriptionStatus.ACTIVE)
    order = add_order_lines(db, subscription_delivery(db, alice, plan, at(-1)), [(water, 5)])

    _dispatch(db, order)

    bot_post.assert_called_once_with(ROUTE, _payload(order, alice, None))


def test_a_delivery_whose_only_line_was_free_offers_no_order_again(db, bot_post, alice, water):
    order = add_order_lines(db, delivered_order(db, alice, at(-1)), reward_lines=[(water, 1)])

    _dispatch(db, order)

    bot_post.assert_called_once_with(ROUTE, _payload(order, alice, None))
