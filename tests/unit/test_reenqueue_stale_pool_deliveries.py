"""Tests for the stale-pool re-enqueue backstop (RC-B / RC-C).

``auto_assign_delivery_task`` gives up after a few retries and only logs that a
"periodic re-enqueue will retry later" — but no such re-enqueue existed, so an
order that missed its assignment window (e.g. created before any driver was
on-shift) stayed invisibly stuck in the pool. ``reenqueue_stale_pool_deliveries``
is that backstop: it re-offers SCHEDULED, driverless deliveries whose order is
still confirmed/preparing and that have sat past the threshold.
"""

import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.user import User
from business_app.tasks.delivery_monitoring_tasks import reenqueue_stale_pool_deliveries
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType

AUTO_ASSIGN = "business_app.tasks.delivery_tasks.auto_assign_delivery_task"


def _user(db):
    user = User(
        email=f"{uuid.uuid4().hex}@t.io",
        phone="+99890" + uuid.uuid4().hex[:7],
        password_hash="x",
        first_name="C",
        last_name="U",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _pool_delivery(
    db,
    *,
    order_status=OrderStatus.CONFIRMED,
    delivery_status=DeliveryStatus.SCHEDULED,
    driver_id=None,
    age_minutes=30,
):
    user = _user(db)
    order = Order(
        user_id=user.id,
        order_number=f"T-{uuid.uuid4().hex[:8]}",
        status=order_status,
        subtotal=Decimal("1000"),
        delivery_fee=Decimal("0"),
        discount_amount=Decimal("0"),
        loyalty_discount=Decimal("0"),
        total_amount=Decimal("1000"),
    )
    db.session.add(order)
    db.session.commit()

    created = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    delivery = Delivery(
        order_id=order.id,
        status=delivery_status,
        delivery_person_id=driver_id,
        scheduled_date=created,
        scheduled_time_slot="09:00-12:00",
        created_at=created,
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery.id


@pytest.mark.unit
class TestReenqueueStalePoolDeliveries:
    def test_reenqueues_stale_confirmed_unassigned(self, app, db):
        delivery_id = _pool_delivery(db, age_minutes=30)
        with patch(AUTO_ASSIGN) as auto:
            result = reenqueue_stale_pool_deliveries.run()
        assert delivery_id in result["delivery_ids"]
        auto.delay.assert_any_call(delivery_id)

    def test_skips_recently_created_delivery(self, app, db):
        """Younger than the threshold — the creation-time auto_assign is still
        in flight, so re-enqueuing now would just race it."""
        delivery_id = _pool_delivery(db, age_minutes=1)
        with patch(AUTO_ASSIGN):
            result = reenqueue_stale_pool_deliveries.run()
        assert delivery_id not in result["delivery_ids"]

    def test_skips_already_assigned_delivery(self, app, db):
        driver = _user(db)
        delivery_id = _pool_delivery(db, driver_id=driver.id, age_minutes=30)
        with patch(AUTO_ASSIGN):
            result = reenqueue_stale_pool_deliveries.run()
        assert delivery_id not in result["delivery_ids"]

    def test_skips_non_scheduled_delivery(self, app, db):
        delivery_id = _pool_delivery(
            db, delivery_status=DeliveryStatus.DELIVERED, age_minutes=30
        )
        with patch(AUTO_ASSIGN):
            result = reenqueue_stale_pool_deliveries.run()
        assert delivery_id not in result["delivery_ids"]

    def test_skips_cancelled_order(self, app, db):
        delivery_id = _pool_delivery(
            db, order_status=OrderStatus.CANCELLED, age_minutes=30
        )
        with patch(AUTO_ASSIGN):
            result = reenqueue_stale_pool_deliveries.run()
        assert delivery_id not in result["delivery_ids"]
