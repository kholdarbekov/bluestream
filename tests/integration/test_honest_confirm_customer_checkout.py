"""The customer checkout confirms the total the screen showed (spec §6.3, plan Task 24).

The bot's confirm screen asks `POST /api/v1/orders/cart/estimate` and posts the order with `expected_total` =
`pricing.final_total` of that answer. When the total has moved since (the switch ran, or the courtesy was lost), the
order is not placed: the route answers 409 `STATE_CHANGED` with `reason` `price_changed` and the new quote, which is
exactly `data` of the estimate for the same checkout.

Every test drives the real routes with the bodies the bot sends. The world comes from `price_courtesy_builders.py`.
"""

from decimal import Decimal

import pytest

from business_app.models.loyalty import LoyaltyPoints, LoyaltyProgram, LoyaltyReward, LoyaltyTransaction
from business_app.models.order import Order
from business_app.utils.constants import LoyaltyTransactionType
from business_app.utils.timezone_utils import get_utc_now
from tests.integration.price_courtesy_builders import (
    BASKET,
    COURTESY_TOTAL,
    courtesy_world,
    entry_of,
    switch,
    water,
)

pytestmark = pytest.mark.integration

ORDERS = "/api/v1/orders/"
ESTIMATE = "/api/v1/orders/cart/estimate"


@pytest.fixture
def client(app):
    """A fresh client per test: the session-scoped one carries JWT cookies from test to test."""
    return app.test_client()


def _estimate_body(customer, product, **extra):
    return {
        "items": [{"product_id": product.id, "quantity": BASKET}],
        "delivery_address_id": customer.address.id,
        "payment_method": "cash",
        **extra,
    }


def _estimate(client, customer, product, **extra):
    response = client.post(ESTIMATE, json=_estimate_body(customer, product, **extra), headers=customer.headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _place(client, customer, product, **extra):
    """The order body the bot posts: the estimate's body plus the channel and `expected_total`."""
    return client.post(
        ORDERS, json={**_estimate_body(customer, product), "source": "telegram", **extra}, headers=customer.headers
    )


def _orders_of(db, user):
    db.session.expire_all()
    return Order.query.filter(Order.user_id == user.id).all()


def _loyalty_account(db, user, points=1000):
    """A loyalty account backed by a real EARNED lot, as `test_order_reward_redemption.points_account` builds it."""
    program = LoyaltyProgram(name="Default", is_active=True, is_default=True)
    db.session.add(program)
    db.session.flush()
    db.session.add(LoyaltyPoints(user_id=user.id, program_id=program.id, total_earned=points, current_balance=points))
    lot = LoyaltyTransaction(
        user_id=user.id,
        transaction_type=LoyaltyTransactionType.EARNED,
        points=points,
        remaining_points=points,
        description="seed",
    )
    lot.expires_at = get_utc_now().replace(year=2999)
    db.session.add(lot)
    db.session.commit()
    return program


@pytest.fixture
def product(db, sample_product):
    return water(db, sample_product)


def test_a_total_drawn_before_the_switch_is_refused_with_the_new_quote(app, db, client, product, admin_user):
    w = courtesy_world(app, db, product, admin_user, switched=False)
    customer = w.customer
    shown = _estimate(client, customer, product)["pricing"]["final_total"]
    assert shown == 72000.0

    switch(db)
    new_quote = _estimate(client, customer, product)
    before = len(_orders_of(db, customer.user))

    response = _place(client, customer, product, expected_total=shown)

    assert response.status_code == 409
    body = response.get_json()
    assert body["success"] is False
    assert body["data"]["reason_code"] == "STATE_CHANGED"
    assert body["data"]["reason"] == "price_changed"
    assert body["data"]["quote"] == new_quote
    assert body["data"]["quote"]["pricing"]["final_total"] == 76000.0
    assert body["data"]["quote"]["items"][0]["courtesy_quantity"] == 2
    assert body["data"]["quote"]["price_courtesy"]["applied"] is True
    assert len(_orders_of(db, customer.user)) == before
    assert entry_of(db, customer.user).order_id is None

    again = _place(client, customer, product, expected_total=76000.0)
    assert again.status_code == 201, again.get_json()


def test_an_ineligible_customer_across_the_switch_is_refused_with_the_new_price(app, db, client, product, admin_user):
    w = courtesy_world(app, db, product, admin_user, switched=False, eligible=False)
    customer = w.customer
    shown = _estimate(client, customer, product)["pricing"]["final_total"]
    assert shown == 72000.0
    switch(db)
    before = len(_orders_of(db, customer.user))

    response = _place(client, customer, product, expected_total=shown)

    assert response.status_code == 409
    data = response.get_json()["data"]
    assert data["reason"] == "price_changed"
    assert data["quote"]["pricing"]["final_total"] == 80000.0
    assert data["quote"]["price_courtesy"] is None
    assert len(_orders_of(db, customer.user)) == before

    assert _place(client, customer, product, expected_total=80000.0).status_code == 201


@pytest.mark.parametrize("expected_total", [76000.0, None])
def test_a_matching_or_absent_expected_total_places_the_order(app, db, client, product, admin_user, expected_total):
    w = courtesy_world(app, db, product, admin_user)
    extra = {} if expected_total is None else {"expected_total": expected_total}

    response = _place(client, w.customer, product, **extra)

    assert response.status_code == 201, response.get_json()
    db.session.expire_all()
    order = db.session.get(Order, response.get_json()["data"]["order"]["id"])
    assert order.total_amount == COURTESY_TOTAL


def test_the_check_reads_the_total_after_the_reward(app, db, client, product, admin_user):
    w = courtesy_world(app, db, product, admin_user)
    program = _loyalty_account(db, w.customer.user)
    reward = LoyaltyReward(
        program_id=program.id,
        name="1000 off",
        reward_type="discount",
        discount_type="fixed",
        discount_value=Decimal("1000.00"),
        points_cost=100,
        is_active=True,
    )
    db.session.add(reward)
    db.session.commit()
    shown = _estimate(client, w.customer, product, reward_id=reward.id)["pricing"]["final_total"]
    assert shown == 75000.0

    response = _place(client, w.customer, product, reward_id=reward.id, expected_total=shown)

    assert response.status_code == 201, response.get_json()
    db.session.expire_all()
    order = db.session.get(Order, response.get_json()["data"]["order"]["id"])
    assert order.loyalty_discount == Decimal("1000.00")
    assert order.total_amount == Decimal("75000.00")


def test_a_free_product_reward_on_a_courtesy_order_keeps_the_courtesy_on_the_paid_line(
    app, db, client, product, admin_user
):
    w = courtesy_world(app, db, product, admin_user)
    program = _loyalty_account(db, w.customer.user)
    reward = LoyaltyReward(
        program_id=program.id,
        name="1 free bottle",
        reward_type="free_product",
        points_cost=100,
        free_product_id=product.id,
        free_product_quantity=1,
        is_active=True,
    )
    db.session.add(reward)
    db.session.commit()
    shown = _estimate(client, w.customer, product, reward_id=reward.id)["pricing"]["final_total"]

    response = _place(client, w.customer, product, reward_id=reward.id, expected_total=shown)

    assert response.status_code == 201, response.get_json()
    db.session.expire_all()
    order = db.session.get(Order, response.get_json()["data"]["order"]["id"])
    paid = [i for i in order.order_items if not i.is_reward_item]
    free = [i for i in order.order_items if i.is_reward_item]
    assert len(paid) == 1 and len(free) == 1
    assert paid[0].courtesy_quantity == 2
    assert paid[0].total_price == Decimal("76000.00")
    assert free[0].courtesy_quantity is None
    assert order.total_amount == Decimal(str(shown))


@pytest.mark.parametrize("bad", ["abc", -1, "76000.001"])
def test_an_unreadable_expected_total_is_refused(app, db, client, product, admin_user, bad):
    w = courtesy_world(app, db, product, admin_user)
    before = len(_orders_of(db, w.customer.user))

    response = _place(client, w.customer, product, expected_total=bad)

    assert response.status_code == 400
    assert len(_orders_of(db, w.customer.user)) == before
