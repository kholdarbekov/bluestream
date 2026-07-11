"""Regression tests for the Subscription.subscription_items relationship name.

P0: ``process_daily_subscription_billing`` crashed daily with
"'Subscription' object has no attribute 'items'" because
``SubscriptionService`` referenced ``subscription.items`` while the model
relationship is named ``subscription_items``
(business_app/models/subscription.py).

The stubs below deliberately mirror the real model: they expose
``subscription_items`` and — like the model — have NO ``items`` attribute,
so the pre-fix code fails with the same AttributeError seen in production.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from business_app.services.subscription_service import SubscriptionService
from shared.enums import PaymentMethod, SubscriptionFrequency, SubscriptionStatus


def _billing_subscription():
    item = SimpleNamespace(product_id=7, quantity=3)
    # Mirror the real model: address is a UserAddress relationship reached via
    # delivery_address_id, money is billing_amount, cadence is billing_cycle.
    address = SimpleNamespace(
        street_address="Amir Temur 1",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        delivery_instructions=None,
    )
    return SimpleNamespace(
        id=42,
        user_id=5,
        status=SubscriptionStatus.ACTIVE,
        last_billing_date=None,
        next_billing_date=datetime.now(timezone.utc),
        subscription_items=[item],
        delivery_address_id=314,
        delivery_address=address,
        # Real column is a NOT NULL PaymentMethod enum (see business_app/models/
        # subscription.py); the stub must mirror that now that billing reads
        # subscription.payment_method.value to build order_data.
        payment_method=PaymentMethod.CARD,
        payment_token=None,
        billing_amount=Decimal("50000.00"),
        total_amount_billed=Decimal("0.00"),
        billing_cycle=SubscriptionFrequency.WEEKLY,
        total_orders_generated=0,
    )


def test_process_subscription_billing_builds_order_items_from_subscription_items(app):
    subscription = _billing_subscription()

    with (
        patch("business_app.services.subscription_service.Subscription") as subscription_model,
        patch("business_app.services.subscription_service.db") as db_mock,
        patch("business_app.services.order_service.OrderService") as order_service_cls,
        patch("business_app.services.payment_service.PaymentService") as payment_service_cls,
    ):
        subscription_model.query.filter_by.return_value.with_for_update.return_value.first.return_value = subscription
        # _already_billed_this_cycle (task 5) now also asks the orders table
        # whether one was created today for this subscription_id, via
        # db.session.query(...).scalar(). db is mocked wholesale here, so
        # that call would otherwise return a truthy MagicMock and make a
        # never-billed subscription look already-billed, short-circuiting
        # before create_order is ever reached. Tell the mock there is none.
        db_mock.session.query.return_value.scalar.return_value = False
        create_order = order_service_cls.return_value.create_order
        # total_amount is required now: billing reads order.total_amount (the
        # Order is authoritative) instead of the old separate create_payment call.
        create_order.return_value = SimpleNamespace(id=99, total_amount=Decimal("50000.00"))
        payment_service_cls.return_value.create_payment.return_value = SimpleNamespace(id=11)

        result = SubscriptionService().process_subscription_billing(42)

    # Pre-fix this never gets here: building order_data raises AttributeError
    # on ``subscription.items`` before create_order is called.
    assert create_order.call_count == 1
    order_data = create_order.call_args[0][1]
    assert order_data["items"] == [{"product_id": 7, "quantity": 3}]
    # create_order subscripts delivery_address["delivery_address_id"], so the
    # billing path must supply it from the subscription FK.
    assert order_data["delivery_address"]["delivery_address_id"] == 314
    assert result["success"] is True
    assert result["order_id"] == 99


def _stats_subscription():
    product = SimpleNamespace(name="Water 19L")
    item = SimpleNamespace(product=product, quantity=2)
    return SimpleNamespace(
        status=SubscriptionStatus.ACTIVE,
        created_at=datetime.now(timezone.utc) - timedelta(days=14),
        frequency="weekly",
        # Subscription has NO total_amount column (business_app/models/
        # subscription.py) — only billing_amount / total_amount_billed. This
        # stub used to expose `total_amount` instead, which meant it silently
        # mirrored the phantom-read bug in calculate_subscription_statistics
        # rather than catching it: the real ORM object has no such attribute.
        billing_amount=Decimal("50000.00"),
        subscription_items=[item],
    )


def test_calculate_subscription_statistics_counts_products_from_subscription_items(app):
    with patch("business_app.services.subscription_service.Subscription") as subscription_model:
        subscription_model.query.filter_by.return_value.all.return_value = [_stats_subscription()]

        stats = SubscriptionService().calculate_subscription_statistics(user_id=5)

    assert stats["total_subscriptions"] == 1
    assert stats["active_subscriptions"] == 1
    assert stats["most_ordered_product"] == "Water 19L"
    # days_active=14, weekly => estimated_deliveries=2; billing_amount=50000.00
    # per delivery => total_spent = 100000.0 (Task 20: was AttributeError on
    # the nonexistent `total_amount` attribute pre-fix).
    assert stats["total_spent"] == 100000.0
    assert stats["average_order_value"] == 50000.0
