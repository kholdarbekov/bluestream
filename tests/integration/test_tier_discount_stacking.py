"""The tier discount stacks additively with the other two discounts.

    total = subtotal - discount_amount (subscription %)
                     - loyalty_discount (redeemed reward)
                     - tier_discount    (this feature)
                     + delivery_fee

and is CLAMPED LAST, so no combination of independently-configurable rates can
drive the total negative or violate ck_orders_tier_discount_nonneg.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app import db as _db
from business_app.models.loyalty import LoyaltyReward, RewardRedemption
from business_app.models.order import Order
from business_app.models.subscription import Subscription
from business_app.services.order_service import OrderService
from business_app.utils.exceptions import ValidationError
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


def test_subscription_and_tier_stack_on_a_generated_cod_order(
    app, db, customer, sample_product, user_address, program
):
    """A subscription-generated order has no HTTP client — billing calls
    OrderService.create_order(subscription=...) directly, so that IS its real
    path."""
    seed_tier(db, program, name="Base", rate=TIER_RATE)
    subscription = Subscription(
        user_id=customer.id,
        name="Weekly Water",
        status=SubscriptionStatus.ACTIVE,
        billing_cycle=SubscriptionFrequency.WEEKLY,
        delivery_frequency=SubscriptionFrequency.WEEKLY,
        delivery_address_id=user_address.id,
        payment_method=PaymentMethod.CASH,
        auto_renew=True,
        discount_percentage=SUBSCRIPTION_RATE,
        billing_amount=Decimal("0.00"),
        start_date=datetime.now(UTC),
        next_billing_date=datetime.now(UTC) - timedelta(minutes=1),
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
    assert Decimal(str(order.tier_discount)) > Decimal("0.00")
    assert Decimal(str(order.total_amount)) == compute_order_total(
        subtotal=Decimal(str(order.subtotal)),
        discount_amount=Decimal(str(order.discount_amount)),
        delivery_fee=Decimal(str(order.delivery_fee or 0)),
        loyalty_discount=Decimal("0.00"),
        tier_discount=Decimal(str(order.tier_discount)),
    )


def test_clamp_holds_the_discount_to_the_headroom_the_others_left(
    app, db, customer, sample_product, user_address, program, monkeypatch
):
    """A 90% tier on top of a 50% subscription. The raw quote exceeds what is
    left of the subtotal, so the clamp binds and the total lands exactly on the
    delivery fee — positive, and both money columns non-negative."""
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", 3000)
    seed_tier(db, program, name="Greedy", rate=GREEDY_TIER_RATE)
    subscription = Subscription(
        user_id=customer.id,
        name="Half price",
        status=SubscriptionStatus.ACTIVE,
        billing_cycle=SubscriptionFrequency.WEEKLY,
        delivery_frequency=SubscriptionFrequency.WEEKLY,
        delivery_address_id=user_address.id,
        payment_method=PaymentMethod.CASH,
        auto_renew=True,
        discount_percentage=SUBSCRIPTION_RATE,
        billing_amount=Decimal("0.00"),
        start_date=datetime.now(UTC),
        next_billing_date=datetime.now(UTC) - timedelta(minutes=1),
    )
    db.session.add(subscription)
    db.session.commit()

    order = OrderService().create_order(
        customer.id,
        _service_order_data(sample_product, user_address, "cash"),
        subscription=subscription,
    )
    order = _reload(order.id)

    raw_quote = (Decimal(str(order.subtotal)) * GREEDY_TIER_RATE / Decimal("100")).quantize(Decimal("0.01"))
    assert Decimal(str(order.tier_discount)) < raw_quote  # the clamp actually bound
    assert Decimal(str(order.tier_discount)) >= Decimal("0.00")
    assert Decimal(str(order.total_amount)) == Decimal(str(order.delivery_fee))
    assert Decimal(str(order.total_amount)) > Decimal("0.00")


def test_rates_past_100_percent_are_refused_rather_than_written_negative(
    app, db, customer, sample_product, user_address, program
):
    """With a free delivery fee the clamped total lands on zero, and
    create_order refuses a non-positive total. No row, no negative column."""
    seed_tier(db, program, name="Greedy", rate=GREEDY_TIER_RATE)
    subscription = Subscription(
        user_id=customer.id,
        name="Half price",
        status=SubscriptionStatus.ACTIVE,
        billing_cycle=SubscriptionFrequency.WEEKLY,
        delivery_frequency=SubscriptionFrequency.WEEKLY,
        delivery_address_id=user_address.id,
        payment_method=PaymentMethod.CASH,
        auto_renew=True,
        discount_percentage=SUBSCRIPTION_RATE,
        billing_amount=Decimal("0.00"),
        start_date=datetime.now(UTC),
        next_billing_date=datetime.now(UTC) - timedelta(minutes=1),
    )
    db.session.add(subscription)
    db.session.commit()

    with pytest.raises(ValidationError, match="positive"):
        OrderService().create_order(
            customer.id,
            _service_order_data(sample_product, user_address, "cash"),
            subscription=subscription,
        )

    assert Order.query.filter_by(user_id=customer.id).count() == 0


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
