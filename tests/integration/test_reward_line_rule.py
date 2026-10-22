"""One rule for "is this line a free reward": the `order_items.is_reward_item` column (spec
2026-10-21 §4, R11). The customer order detail, the admin order detail and the admin Orders list
all publish it as `is_reward`, and `has_loyalty_reward` follows it. A zero price on its own (a
0 UZS contract line) is not a reward.

Driven through the routes the bot (`GET /api/v1/orders/<id>`) and the admin UI
(`GET /api/v1/admin/orders[/<id>]`) call. The reward line is the row
`LoyaltyService.apply_reward_to_order` writes (loyalty_service.py:1645-1653), built by
`add_order_lines(reward_lines=...)`.
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from business_app.models.order import OrderItem
from tests.integration.reorder_reminder_builders import (
    add_order_lines,
    at,
    customer_headers,
    delivered_order,
    make_customer,
    make_product,
)

pytestmark = pytest.mark.integration

ZERO = Decimal("0.00")


def _zero_contract_line(db, order, product, qty):
    """The line a 0 UZS contract price leaves: priced 0 and NOT a reward."""
    db.session.add(
        OrderItem(order_id=order.id, product_id=product.id, quantity=qty,
                  unit_price=ZERO, total_price=ZERO, is_reward_item=False)
    )
    db.session.commit()
    return order


@pytest.fixture
def orders(db, sample_category):
    alice = make_customer(db, first_name="Alice")
    water = make_product(db, sample_category, "Water 19L")
    cups = make_product(db, sample_category, "Cooler cups")
    mixed = add_order_lines(db, delivered_order(db, alice, at(-3)), [(water, 5)], reward_lines=[(water, 1)])
    _zero_contract_line(db, mixed, cups, 2)
    contract_only = _zero_contract_line(db, delivered_order(db, alice, at(-1)), cups, 4)
    return SimpleNamespace(alice=alice, water=water, cups=cups, mixed=mixed, contract_only=contract_only)


def _flags(items):
    return sorted((item["product_id"], item["quantity"], item["is_reward"]) for item in items)


def _mixed_flags(o):
    return sorted([(o.water.id, 5, False), (o.water.id, 1, True), (o.cups.id, 2, False)])


def test_the_customer_order_detail_flags_only_the_column_marked_line(app, client, orders):
    resp = client.get(f"/api/v1/orders/{orders.mixed.id}", headers=customer_headers(app, orders.alice))

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert _flags(resp.get_json()["data"]["order"]["order_items"]) == _mixed_flags(orders)


def test_the_admin_order_detail_flags_the_same_lines(client, orders, admin_claim_headers):
    resp = client.get(f"/api/v1/admin/orders/{orders.mixed.id}", headers=admin_claim_headers)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()["data"]["order"]
    assert (_flags(body["items"]), body["has_loyalty_reward"]) == (_mixed_flags(orders), True)


def test_a_zero_priced_contract_order_is_no_loyalty_reward_on_the_admin_screens(client, orders, admin_claim_headers):
    detail = client.get(f"/api/v1/admin/orders/{orders.contract_only.id}", headers=admin_claim_headers)
    listing = client.get("/api/v1/admin/orders", headers=admin_claim_headers)

    assert (detail.status_code, listing.status_code) == (200, 200)
    body = detail.get_json()["data"]["order"]
    row = next(r for r in listing.get_json()["data"]["items"] if r["id"] == orders.contract_only.id)
    assert (_flags(body["items"]), body["has_loyalty_reward"]) == ([(orders.cups.id, 4, False)], False)
    assert (_flags(row["items_summary"]), row["has_loyalty_reward"]) == ([(orders.cups.id, 4, False)], False)
