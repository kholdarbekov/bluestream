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
from shared.enums import SubscriptionFrequency, SubscriptionStatus


def _billing_subscription():
    item = SimpleNamespace(product_id=7, quantity=3)
    return SimpleNamespace(
        id=42,
        user_id=5,
        status=SubscriptionStatus.ACTIVE,
        last_billing_date=None,
        next_billing_date=datetime.now(timezone.utc),
        subscription_items=[item],
        delivery_address_street="Amir Temur 1",
        delivery_address_city="Tashkent",
        delivery_address_latitude=41.31,
        delivery_address_longitude=69.28,
        delivery_instructions=None,
        payment_method="card",
        payment_token=None,
        total_amount=Decimal("50000.00"),
        frequency=SubscriptionFrequency.WEEKLY,
        billing_cycle_count=0,
        last_order_id=None,
    )


def test_process_subscription_billing_builds_order_items_from_subscription_items(app):
    subscription = _billing_subscription()

    with (
        patch("business_app.services.subscription_service.Subscription") as subscription_model,
        patch("business_app.services.subscription_service.db"),
        patch("business_app.services.order_service.OrderService") as order_service_cls,
        patch("business_app.services.payment_service.PaymentService") as payment_service_cls,
    ):
        subscription_model.query.filter_by.return_value.with_for_update.return_value.first.return_value = subscription
        create_order = order_service_cls.return_value.create_order
        create_order.return_value = SimpleNamespace(id=99)
        payment_service_cls.return_value.create_payment.return_value = SimpleNamespace(id=11)

        result = SubscriptionService().process_subscription_billing(42)

    # Pre-fix this never gets here: building order_data raises AttributeError
    # on ``subscription.items`` before create_order is called.
    assert create_order.call_count == 1
    order_data = create_order.call_args[0][1]
    assert order_data["items"] == [{"product_id": 7, "quantity": 3}]
    assert result["success"] is True
    assert result["order_id"] == 99


def _stats_subscription():
    product = SimpleNamespace(name="Water 19L")
    item = SimpleNamespace(product=product, quantity=2)
    return SimpleNamespace(
        status=SubscriptionStatus.ACTIVE,
        created_at=datetime.now(timezone.utc) - timedelta(days=14),
        frequency="weekly",
        # float, not Decimal: the method mixes total_spent with float literals
        total_amount=50000.0,
        subscription_items=[item],
    )


def test_calculate_subscription_statistics_counts_products_from_subscription_items(app):
    with patch("business_app.services.subscription_service.Subscription") as subscription_model:
        subscription_model.query.filter_by.return_value.all.return_value = [_stats_subscription()]

        stats = SubscriptionService().calculate_subscription_statistics(user_id=5)

    assert stats["total_subscriptions"] == 1
    assert stats["active_subscriptions"] == 1
    assert stats["most_ordered_product"] == "Water 19L"
