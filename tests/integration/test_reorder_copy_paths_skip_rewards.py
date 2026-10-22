"""Paths that copy an old order's lines never copy its free reward line (spec 2026-10-21 §4, R4).
Covered here: "Your usual" (GET /api/v1/orders/quick-reorder) and retry-with-cash
(POST /api/v1/orders/<id>/retry-with-cash, the bot's rescue at telegram_bot/handlers/orders.py:1593).
The sales-agent "Same as last order" quantity is pinned next to its route in
test_staff_sales_visits_api.py. Every test drives the route the client calls, with the customer's JWT.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.order import Order, OrderStatusHistory
from shared.enums import OrderStatus, PaymentMethod
from tests.integration.reorder_reminder_builders import (
    add_order_lines,
    customer_headers,
    delivered_order,
    live_order,
    make_customer,
    make_product,
)

pytestmark = pytest.mark.integration


def test_your_usual_counts_the_paid_lines_of_delivered_orders_only(app, client, db, sample_category):
    alice = make_customer(db)
    water = make_product(db, sample_category, "Water 19L")
    cups = make_product(db, sample_category, "Cooler cups")
    now = datetime.now(UTC)
    add_order_lines(db, delivered_order(db, alice, now - timedelta(days=10)), [(water, 4)],
                    reward_lines=[(water, 2), (cups, 1)])
    add_order_lines(db, delivered_order(db, alice, now - timedelta(days=3)), [(water, 6)])
    add_order_lines(db, live_order(db, alice, now - timedelta(days=1), status=OrderStatus.CONFIRMED), [(water, 1)])

    resp = client.get("/api/v1/orders/quick-reorder", query_string={"limit": 3, "period_days": 90},
                      headers=customer_headers(app, alice))

    assert resp.status_code == 200, resp.get_data(as_text=True)
    rows = [(s["product_id"], s["suggested_quantity"], s["order_frequency"], s["total_ordered"])
            for s in resp.get_json()["data"]["quick_reorder_suggestions"]]
    # 4 + 6 paid over 2 delivered orders. The free water, the free cups (never bought) and the
    # still-CONFIRMED order count for nothing.
    assert rows == [(water.id, 5, 2, 10)]


def _cancelled_by_tax_committee(db, user, address, lines, reward_lines=()):
    """The Click order `create_order` cancelled while Asl belgisi was down. It is CANCELLED and
    carries the `tax_committee_unavailable` history note that the rescue checks for."""
    order = live_order(db, user, datetime.now(UTC) - timedelta(minutes=5),
                       status=OrderStatus.CANCELLED, address=address)
    order.payment_method = PaymentMethod.CLICK
    db.session.add(OrderStatusHistory(order_id=order.id, old_status=OrderStatus.PENDING,
                                      new_status=OrderStatus.CANCELLED, notes="tax_committee_unavailable"))
    db.session.commit()
    return add_order_lines(db, order, lines, reward_lines=reward_lines)


def test_retry_with_cash_copies_the_paid_lines_only(app, client, db, sample_user, sample_product, user_address):
    """Cancelling the card order already refunded the points (order_service.py:1354-1362), so the
    cash retry copies only what was paid for."""
    cancelled = _cancelled_by_tax_committee(db, sample_user, user_address, [(sample_product, 2)],
                                            reward_lines=[(sample_product, 1)])

    resp = client.post(f"/api/v1/orders/{cancelled.id}/retry-with-cash", headers=customer_headers(app, sample_user))

    assert resp.status_code == 201, resp.get_data(as_text=True)
    rescued = resp.get_json()["data"]["order"]
    assert [(i["product_id"], i["quantity"], i["is_reward"]) for i in rescued["order_items"]] == [
        (sample_product.id, 2, False)
    ]
    assert db.session.get(Order, rescued["id"]).subtotal == Decimal("30000.00")


def test_retry_with_cash_refuses_an_order_whose_only_lines_were_free(
    app, client, db, sample_user, sample_product, user_address
):
    """Two free bottles (30000 UZS at list price) clear MIN_ORDER_AMOUNT (20000), so only the
    no-reorderable-lines guard can refuse this. A min-amount 400 does not satisfy the test."""
    cancelled = _cancelled_by_tax_committee(db, sample_user, user_address, [], reward_lines=[(sample_product, 2)])

    resp = client.post(f"/api/v1/orders/{cancelled.id}/retry-with-cash", headers=customer_headers(app, sample_user))

    assert (resp.status_code, resp.get_json()["message"]) == (400, "Cancelled order has no items to retry")
    assert Order.query.filter_by(user_id=sample_user.id).count() == 1
