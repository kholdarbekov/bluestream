"""POST /api/v1/cart/fill-from-order/<order_id> is one backend call. It fills the cart from an
order's reorderable lines and reports what it could not add (spec 2026-10-21 §3, §4). It replaces
the bot's clear + N adds, which emptied the cart when the first add failed and copied the free
bottle as a paid one. Driven through the route the bot calls, with the customer's JWT. The cart is
read back through GET /api/v1/cart/, the screen the bot draws next.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from business_app.models.loyalty import LoyaltyReward
from business_app.services.loyalty_service import LoyaltyService
from business_app.services.order_service import OrderService
from tests.integration.reorder_reminder_builders import (
    add_order_lines,
    at,
    customer_headers,
    deliver,
    delivered_order,
    make_customer,
    make_product,
)
# The real-redemption fixtures and order payload of the reward-redemption suite.
from tests.integration.test_order_reward_redemption import (  # noqa: F401
    _order_data,
    loyalty_program,
    points_account,
)

pytestmark = pytest.mark.integration


def _fill(app, client, user, order_id):
    return client.post(f"/api/v1/cart/fill-from-order/{order_id}", headers=customer_headers(app, user))


def _cart(app, client, user):
    resp = client.get("/api/v1/cart/", headers=customer_headers(app, user))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    cart = resp.get_json()["data"]["cart"] or {"cart_items": []}
    return sorted((line["product_id"], line["quantity"]) for line in cart["cart_items"])


@pytest.fixture
def last_order(db, sample_category):
    """Alice's delivered order: 5 paid water, 1 free water (the reward line) and 2 pumps."""
    alice = make_customer(db)
    water = make_product(db, sample_category, "Water 19L")
    pump = make_product(db, sample_category, "Hand pump")
    cups = make_product(db, sample_category, "Cooler cups")
    order = add_order_lines(db, delivered_order(db, alice, at(-1)), [(water, 5), (pump, 2)],
                            reward_lines=[(water, 1)])
    return SimpleNamespace(alice=alice, water=water, pump=pump, cups=cups, order=order)


def _cups_already_in_cart(app, client, o):
    resp = client.post("/api/v1/cart/items", json={"product_id": o.cups.id, "quantity": 3},
                       headers=customer_headers(app, o.alice))
    assert resp.status_code == 200, resp.get_data(as_text=True)


def test_it_replaces_the_cart_with_the_paid_lines_only(app, client, last_order):
    o = last_order
    _cups_already_in_cart(app, client, o)

    resp = _fill(app, client, o.alice, o.order.id)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["data"] == {
        "added": [{"product_id": o.water.id, "quantity": 5}, {"product_id": o.pump.id, "quantity": 2}],
        "dropped": [],
    }
    assert _cart(app, client, o.alice) == sorted([(o.water.id, 5), (o.pump.id, 2)])


def test_an_inactive_product_is_dropped_with_its_reason(app, client, db, last_order):
    o = last_order
    o.pump.is_active = False
    db.session.commit()

    assert _fill(app, client, o.alice, o.order.id).get_json()["data"] == {
        "added": [{"product_id": o.water.id, "quantity": 5}],
        "dropped": [{"product_id": o.pump.id, "quantity": 2, "reason": "inactive"}],
    }
    assert _cart(app, client, o.alice) == [(o.water.id, 5)]


def test_when_nothing_can_be_added_the_cart_is_still_emptied(app, client, db, last_order):
    """Order again replaces the cart even when every product is gone. The bot then shows
    `telegram.quick_order.unavailable` and Products (Review Focus 5), never the stale cart."""
    o = last_order
    _cups_already_in_cart(app, client, o)
    o.water.is_active = o.pump.is_active = False
    db.session.commit()

    assert _fill(app, client, o.alice, o.order.id).get_json()["data"] == {
        "added": [],
        "dropped": [
            {"product_id": o.water.id, "quantity": 5, "reason": "inactive"},
            {"product_id": o.pump.id, "quantity": 2, "reason": "inactive"},
        ],
    }
    assert _cart(app, client, o.alice) == []


def test_a_line_the_stock_cannot_cover_is_dropped_as_unavailable(app, client, db, last_order):
    o = last_order
    o.pump.stock_quantity = 1
    db.session.commit()

    assert _fill(app, client, o.alice, o.order.id).get_json()["data"]["dropped"] == [
        {"product_id": o.pump.id, "quantity": 2, "reason": "unavailable"}
    ]
    assert _cart(app, client, o.alice) == [(o.water.id, 5)]


def test_another_customers_order_is_a_404_and_her_cart_is_untouched(app, client, db, last_order):
    o = last_order
    _cups_already_in_cart(app, client, o)
    bob = make_customer(db)
    bobs = add_order_lines(db, delivered_order(db, bob, at(-1)), [(o.water, 9)])

    resp = _fill(app, client, o.alice, bobs.id)

    assert (resp.status_code, resp.get_json()["error"]) == (404, "NOT_FOUND")
    assert _cart(app, client, o.alice) == [(o.cups.id, 3)]


def test_filling_twice_gives_the_same_cart(app, client, last_order):
    o = last_order

    first = _fill(app, client, o.alice, o.order.id).get_json()["data"]
    second = _fill(app, client, o.alice, o.order.id).get_json()["data"]

    assert first == second
    assert _cart(app, client, o.alice) == sorted([(o.water.id, 5), (o.pump.id, 2)])


@pytest.fixture
def free_water_reward(db, loyalty_program, sample_product):  # noqa: F811
    """'One free 19 L': the free line is the same SKU she pays for."""
    reward = LoyaltyReward(program_id=loyalty_program.id, name="1 free bottle", reward_type="free_product",
                           points_cost=100, free_product_id=sample_product.id, free_product_quantity=1,
                           is_active=True)
    db.session.add(reward)
    db.session.commit()
    return reward


def test_a_really_redeemed_free_bottle_is_copied_by_no_reorder_path(
    app, client, db, monkeypatch, sample_user, sample_product, user_address, mock_inventory_service,
    points_account, free_water_reward,  # noqa: F811
):
    """Spec 2026-10-21 Tests: the free line `LoyaltyService.apply_reward_to_order` really writes
    (loyalty_service.py:1645-1653), not a builder's copy of it. If the writer stopped flagging
    it, this test fails while the builder-based ones stay green. Setup is the redemption suite's
    idiom (test_order_reward_redemption.py:255-330)."""
    monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
    mock_inventory_service.check_multiple_products_availability.return_value = [
        SimpleNamespace(product_id=sample_product.id, requested_quantity=2, available_quantity=100,
                        reserved_quantity=0, is_available=True, reason="Available")
    ]
    mock_inventory_service.reserve_inventory.return_value = {"success": True, "expires_at": None}
    with patch(
        "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
        return_value=None,
    ), patch("business_app.services.payment_service.PaymentService.initialize_order_payment", return_value=None):
        order = OrderService(inventory_service=mock_inventory_service).create_order(
            sample_user.id, _order_data(sample_product, user_address, reward_id=free_water_reward.id)
        )
    deliver(db, order, at(-1))
    assert sorted((i.quantity, i.is_reward_item) for i in order.order_items) == [(1, True), (2, False)]
    headers = customer_headers(app, sample_user)

    info = client.get("/api/v1/orders/reorder-info", headers=headers).get_json()["data"]
    usual = client.get("/api/v1/orders/quick-reorder", headers=headers).get_json()["data"]
    filled = _fill(app, client, sample_user, order.id)

    assert info["candidate"]["items"] == [
        {"product_id": sample_product.id, "product_name": "Pure Water 19L", "quantity": 2}
    ]
    assert [(s["product_id"], s["suggested_quantity"], s["total_ordered"])
            for s in usual["quick_reorder_suggestions"]] == [(sample_product.id, 2, 2)]
    assert filled.get_json()["data"] == {"added": [{"product_id": sample_product.id, "quantity": 2}], "dropped": []}
    assert _cart(app, client, sample_user) == [(sample_product.id, 2)]
