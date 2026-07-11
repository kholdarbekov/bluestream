"""Task 19: admin order serializer must surface subscription origin.

Order.subscription_id / Order.is_subscription_order were added in Task 3
(see tests/unit/test_subscription_order_parity.py) so the admin Orders page
can tell a subscription-generated order from an ordinary one, but
serialize_order_admin (business_app/serializers/admin_serializers.py) never
emitted them.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from business_app.models.order import Order
from business_app.models.subscription import Subscription, SubscriptionItem
from business_app.serializers.admin_serializers import serialize_order_admin
from business_app.services.order_service import OrderService
from business_app.services.subscription_service import SubscriptionService
from shared.enums import PaymentMethod, SubscriptionFrequency, SubscriptionStatus


@pytest.fixture
def sample_subscription(db, sample_user, sample_product, user_address):
    """An ACTIVE cash subscription for 2 units of sample_product, 10% discount.

    Mirrors the fixture of the same name in test_subscription_order_parity.py.
    """
    subscription = Subscription(
        user_id=sample_user.id,
        name="Weekly Water",
        status=SubscriptionStatus.ACTIVE,
        billing_cycle=SubscriptionFrequency.WEEKLY,
        delivery_frequency=SubscriptionFrequency.WEEKLY,
        delivery_address_id=user_address.id,
        payment_method=PaymentMethod.CASH,
        auto_renew=True,
        discount_percentage=10.0,
        billing_amount=Decimal("0.00"),
        start_date=datetime.now(timezone.utc),
        next_billing_date=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.session.add(subscription)
    db.session.flush()
    db.session.add(
        SubscriptionItem(
            subscription_id=subscription.id,
            product_id=sample_product.id,
            quantity=2,
            unit_price=sample_product.base_price,
            total_price=sample_product.base_price * 2,
        )
    )
    db.session.commit()
    return subscription


@pytest.mark.unit
class TestSerializeOrderAdminSubscriptionOrigin:
    def test_subscription_generated_order_exposes_origin_fields(self, app, db, sample_subscription):
        with app.app_context():
            result = SubscriptionService().process_subscription_billing(sample_subscription.id)
            order = Order.query.get(result["order_id"])

            data = serialize_order_admin(order)

            assert data["is_subscription_order"] is True
            assert data["subscription_id"] == sample_subscription.id

    def test_ordinary_order_has_falsy_subscription_origin(self, app, db, sample_user, sample_product, user_address):
        with app.app_context():
            order = OrderService().create_order(
                sample_user.id,
                {
                    "items": [{"product_id": sample_product.id, "quantity": 2}],
                    "delivery_address": {
                        "delivery_address_id": user_address.id,
                        "street": "1 Test St",
                        "latitude": 41.3111,
                        "longitude": 69.2797,
                    },
                    "payment_method": "cash",
                },
            )

            data = serialize_order_admin(order)

            assert data["is_subscription_order"] is False
            assert data["subscription_id"] is None
