"""The tier discount stacks additively with the other two discounts.

    total = subtotal - discount_amount (subscription %)
                     - loyalty_discount (redeemed reward)
                     - tier_discount    (this feature)
                     + delivery_fee

and is CLAMPED LAST, so no combination of independently-configurable rates can
drive the total negative or violate ck_orders_tier_discount_nonneg. A
subscription order never carries a tier discount (spec 2026-10-23), so on a
real order the tier meets the reward only.
"""

from datetime import UTC, datetime, timedelta
from business_app.utils.local_windows import local_date
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app import db as _db
from business_app.models.loyalty import LoyaltyReward, RewardRedemption
from business_app.models.order import Order
from business_app.models.subscription import Subscription
from business_app.services.order_service import OrderService
from business_app.utils.order_totals import compute_order_total
from shared.enums import PaymentMethod, SubscriptionFrequency, SubscriptionStatus
from tests.integration.tier_discount_factory import (
    post_order,
    seed_account,
    seed_program,
    seed_tier,
    verify_phone,
)

TIER_RATE = Decimal("7")
GREEDY_TIER_RATE = Decimal("90")
SUBSCRIPTION_RATE = 50.0
REWARD_VALUE = Decimal("500.00")


def _headers(app, user_id):
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _reload(order_id):
    _db.session.expire_all()
    return Order.query.get(order_id)


def _service_order_data(product, address, method):
    """The SERVICE-layer payload shape (nested delivery_address), used only for
    the subscription-generated origin, which has no HTTP client."""
    return {
        "items": [{"product_id": product.id, "quantity": 2}],
        "delivery_address": {
            "delivery_address_id": address.id,
            "street": address.street_address,
            "latitude": address.latitude,
            "longitude": address.longitude,
        },
        "payment_method": method,
    }


@pytest.fixture
def program(db):
    return seed_program(db)


@pytest.fixture
def customer(db, sample_user):
    verify_phone(db, sample_user)
    return sample_user


@pytest.fixture
def discount_reward(db, program):
    reward = LoyaltyReward(
        program_id=program.id,
        name="Fixed reward",
        reward_type="discount",
        discount_type="fixed",
        discount_value=REWARD_VALUE,
        points_cost=100,
        is_active=True,
    )
    db.session.add(reward)
    db.session.commit()
    return reward


def test_reward_and_tier_stack_on_one_cod_order(
    app, db, customer, sample_product, user_address, program, discount_reward, monkeypatch
):
    monkeypatch.setattr(
        "business_app.services.loyalty_service.LoyaltyService._send_points_notification",
        lambda *a, **k: None,
    )
    seed_tier(db, program, name="Base", rate=TIER_RATE)
    seed_account(db, customer, program, qualifying_points=1000, balance=1000)

    resp = post_order(
        app,
        _headers(app, customer.id),
        product_id=sample_product.id,
        address_id=user_address.id,
        payment_method="cash",
        reward_id=discount_reward.id,
    )

    assert resp.status_code == 201, resp.get_json()
    order = _reload(resp.get_json()["data"]["order"]["id"])

    assert Decimal(str(order.loyalty_discount)) == REWARD_VALUE
    assert Decimal(str(order.tier_discount)) > Decimal("0.00")
    assert Decimal(str(order.total_amount)) == compute_order_total(
        subtotal=Decimal(str(order.subtotal)),
        discount_amount=Decimal(str(order.discount_amount or 0)),
        delivery_fee=Decimal(str(order.delivery_fee or 0)),
        loyalty_discount=Decimal(str(order.loyalty_discount)),
        tier_discount=Decimal(str(order.tier_discount)),
    )
    assert RewardRedemption.query.filter_by(order_id=order.id, status="applied").count() == 1


def test_a_subscription_order_keeps_its_subscription_discount_and_gets_no_tier(
    app, db, customer, sample_product, user_address, program
):
    """A subscription-generated order has no HTTP client — billing calls
    OrderService.create_order(subscription=...) directly, so that IS its real
    path. The tier discount is a perk of the customer's own bot or website order
    (spec 2026-10-23), so a subscription order keeps its subscription price and
    never stacks the tier on top."""
    seed_tier(db, program, name="Base", rate=TIER_RATE)
    subscription = Subscription(
        user_id=customer.id,
        name="Weekly Water",
        status=SubscriptionStatus.ACTIVE,
        frequency=SubscriptionFrequency.WEEKLY,
        delivery_address_id=user_address.id,
        payment_method=PaymentMethod.CASH,
        auto_renew=True,
        discount_percentage=SUBSCRIPTION_RATE,
        billing_amount=Decimal("0.00"),
        start_date=datetime.now(UTC),
        next_delivery_date=local_date(),
    )
    db.session.add(subscription)
    db.session.commit()

    order = OrderService().create_order(
        customer.id,
        _service_order_data(sample_product, user_address, "cash"),
        subscription=subscription,
    )
    order = _reload(order.id)

    assert Decimal(str(order.discount_amount)) > Decimal("0.00")
    assert Decimal(str(order.tier_discount)) == Decimal("0.00")
    assert Decimal(str(order.total_amount)) == compute_order_total(
        subtotal=Decimal(str(order.subtotal)),
        discount_amount=Decimal(str(order.discount_amount)),
        delivery_fee=Decimal(str(order.delivery_fee or 0)),
        loyalty_discount=Decimal("0.00"),
        tier_discount=Decimal("0.00"),
    )


def test_a_large_reward_pushes_the_tier_discount_down_not_the_total_negative(
    app, db, customer, sample_product, user_address, program, monkeypatch
):
    """The reward is applied AFTER the order was priced. Without the re-clamp
    in apply_reward_to_order the tier discount would stay at its creation value
    and the total would go negative."""
    monkeypatch.setattr(
        "business_app.services.loyalty_service.LoyaltyService._send_points_notification",
        lambda *a, **k: None,
    )
    seed_tier(db, program, name="Greedy", rate=GREEDY_TIER_RATE)
    seed_account(db, customer, program, qualifying_points=1000, balance=1000)
    big_reward = LoyaltyReward(
        program_id=program.id,
        name="Almost everything",
        reward_type="discount",
        discount_type="percentage",
        discount_value=Decimal("80"),
        points_cost=100,
        is_active=True,
    )
    db.session.add(big_reward)
    db.session.commit()

    resp = post_order(
        app,
        _headers(app, customer.id),
        product_id=sample_product.id,
        address_id=user_address.id,
        payment_method="cash",
        reward_id=big_reward.id,
    )

    assert resp.status_code == 201, resp.get_json()
    order = _reload(resp.get_json()["data"]["order"]["id"])

    assert Decimal(str(order.tier_discount)) >= Decimal("0.00")
    assert Decimal(str(order.total_amount)) >= Decimal("0.00")
    assert Decimal(str(order.total_amount)) == compute_order_total(
        subtotal=Decimal(str(order.subtotal)),
        discount_amount=Decimal(str(order.discount_amount or 0)),
        delivery_fee=Decimal(str(order.delivery_fee or 0)),
        loyalty_discount=Decimal(str(order.loyalty_discount)),
        tier_discount=Decimal(str(order.tier_discount)),
    )
