"""Regression tests for the Subscription.subscription_items relationship name.

P0: ``process_daily_subscription_billing`` crashed daily with
"'Subscription' object has no attribute 'items'" because
``SubscriptionService`` referenced ``subscription.items`` while the model
relationship is named ``subscription_items``
(business_app/models/subscription.py).

Driven against real rows: the order the engine creates carries exactly the
subscription's items and its delivery address.
"""

from business_app.models.order import Order, OrderItem
from business_app.services.subscription_service import SubscriptionService

# An ACTIVE cash subscription for 2 units of sample_product, due today.
from tests.unit.test_subscription_order_parity import sample_subscription  # noqa: F401


def test_create_next_order_builds_order_items_from_subscription_items(
    app, db, sample_user, sample_product, user_address, sample_subscription
):
    with app.app_context():
        result = SubscriptionService().create_next_order(sample_subscription.id)

        assert result["success"] is True
        order = Order.query.get(result["order_id"])
        lines = [(line.product_id, line.quantity) for line in OrderItem.query.filter_by(order_id=order.id)]
        assert lines == [(sample_product.id, 2)]
        # create_order subscripts delivery_address["delivery_address_id"]; it comes from the FK.
        assert order.delivery_address_id == user_address.id
