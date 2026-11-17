"""Admin edits of a courtesy line: preview and apply go through `line_money` (spec 2026-11-16 §5.7).

A courtesy line stores `unit_price x quantity != total_price`, so any edit of such an order that
multiplied every line would preview an inflated subtotal and a phantom payment delta. The tests drive the
routes the order-edit modal calls, with the body it sends (`Orders.js`).
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.order import Order, OrderEditHistory, OrderItem
from business_app.models.price_change import PriceChange, PriceChangeItem, PriceChangeStatus
from business_app.models.product import Product
from shared.enums import OrderStatus, PaymentMethod

pytestmark = pytest.mark.integration

COURTESY_PRICE = Decimal("18000.00")


@pytest.fixture
def courtesy_order(db, sample_user, sample_product):
    """A CONFIRMED unpaid cash order: 4 x 20,000 with 2 units at the old 18,000 (net 76,000)."""
    sample_product.base_price = Decimal("20000.00")
    order = Order(
        user_id=sample_user.id,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("76000.00"),
        discount_amount=Decimal("0.00"),
        delivery_fee=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        tier_discount=Decimal("0.00"),
        total_amount=Decimal("76000.00"),
        payment_method=PaymentMethod.CASH,
        is_paid=False,
        created_at=datetime.now(UTC),
    )
    _db.session.add(order)
    _db.session.flush()
    _db.session.add(
        OrderItem.from_priced_line(
            order.id,
            {
                "product_id": sample_product.id,
                "quantity": 4,
                "unit_price": Decimal("20000.00"),
                "discount_amount": Decimal("4000.00"),
                "total_price": Decimal("76000.00"),
                "courtesy_quantity": 2,
                "courtesy_unit_price": COURTESY_PRICE,
            },
        )
    )
    _db.session.commit()
    return order


def _body(order, product, quantity):
    return {
        "items": [{"orderItemId": order.order_items[0].id, "productId": product.id, "quantity": quantity}],
        "reason": "customer asked",
    }


def _preview(client, headers, order, body):
    resp = client.post(f"/api/v1/admin/orders/{order.id}/edit-preview", json=body, headers=headers)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]


def _apply(client, headers, order, body):
    resp = client.post(f"/api/v1/admin/orders/{order.id}/edit", json=body, headers=headers)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]


def _figures(line):
    return (
        line["quantity"],
        line["courtesy_quantity"],
        line["courtesy_unit_price"],
        line["regular_quantity"],
        line["discount_amount"],
        line["total_price"],
    )


def test_preview_raising_a_courtesy_line_adds_units_at_the_line_price(
    client, admin_auth_headers, courtesy_order, sample_product
):
    data = _preview(client, admin_auth_headers, courtesy_order, _body(courtesy_order, sample_product, 6))

    assert _figures(data["items_after"][0]) == (6, 2, 18000.0, 4, 4000.0, 116000.0)
    assert data["totals_after"]["subtotal"] == 116000.0
    assert data["totals_after"]["total_amount"] == 116000.0


@pytest.mark.parametrize(
    "quantity, courtesy, discount, total",
    [(3, 2, 4000.0, 56000.0), (1, 1, 2000.0, 18000.0)],
)
def test_lowering_keeps_or_shrinks_the_courtesy_units(
    client, admin_auth_headers, courtesy_order, sample_product, quantity, courtesy, discount, total
):
    body = _body(courtesy_order, sample_product, quantity)
    expected = (quantity, courtesy, 18000.0, quantity - courtesy, discount, total)

    preview = _preview(client, admin_auth_headers, courtesy_order, body)
    assert _figures(preview["items_after"][0]) == expected

    _apply(client, admin_auth_headers, courtesy_order, body)
    item = Order.query.get(courtesy_order.id).order_items[0]
    assert (
        item.quantity,
        item.courtesy_quantity,
        float(item.courtesy_unit_price),
        item.quantity - item.courtesy_quantity,
        float(item.discount_amount),
        float(item.total_price),
    ) == expected


@pytest.fixture
def ten_litre(db, sample_category):
    product = Product(
        name="Water 10L",
        description="d",
        category_id=sample_category.id,
        size="10L",
        volume=10.0,
        volume_unit="L",
        base_price=Decimal("12000.00"),
        stock_quantity=100,
        min_stock_level=1,
        max_stock_level=500,
        is_active=True,
    )
    db.session.add(product)
    db.session.commit()
    return product


def _add_line_body(order, product, ten_litre, quantity):
    body = _body(order, product, 4)
    body["items"].append({"productId": ten_litre.id, "quantity": quantity})
    return body


def test_an_untouched_courtesy_line_keeps_its_net_total(
    client, admin_auth_headers, courtesy_order, sample_product, ten_litre
):
    data = _preview(
        client, admin_auth_headers, courtesy_order, _add_line_body(courtesy_order, sample_product, ten_litre, 2)
    )

    courtesy_line, new_line = data["items_after"]
    assert courtesy_line["total_price"] == 76000.0
    assert courtesy_line["courtesy_quantity"] == 2
    assert (new_line["courtesy_quantity"], new_line["discount_amount"], new_line["total_price"]) == (
        None,
        0.0,
        24000.0,
    )
    assert data["totals_after"]["subtotal"] == 100000.0
    assert data["cascade_summary"]["payment"]["total_delta"] == 24000.0


def test_a_product_added_by_an_edit_gets_no_courtesy_even_inside_the_change(
    client, admin_auth_headers, courtesy_order, sample_product, ten_litre, sample_user
):
    change = PriceChange(
        announce_on=date(2026, 11, 1),
        change_on=date(2026, 11, 10),
        status=PriceChangeStatus.APPLIED,
        applied_at=datetime.now(UTC),
        created_by_id=sample_user.id,
    )
    change.items = [
        PriceChangeItem(product_id=ten_litre.id, new_price=Decimal("12000.00"), courtesy_cap=2, old_price=Decimal("11000.00"))
    ]
    _db.session.add(change)
    _db.session.commit()

    _apply(client, admin_auth_headers, courtesy_order, _add_line_body(courtesy_order, sample_product, ten_litre, 2))

    added = OrderItem.query.filter_by(order_id=courtesy_order.id, product_id=ten_litre.id).one()
    assert added.courtesy_quantity is None
    assert added.courtesy_unit_price is None
    assert added.total_price == added.unit_price * added.quantity


def test_apply_raise_keeps_the_courtesy(client, admin_auth_headers, courtesy_order, sample_product):
    _apply(client, admin_auth_headers, courtesy_order, _body(courtesy_order, sample_product, 6))

    order = Order.query.get(courtesy_order.id)
    item = order.order_items[0]
    assert (
        item.quantity,
        item.courtesy_quantity,
        item.courtesy_unit_price,
        item.discount_amount,
        item.total_price,
    ) == (6, 2, Decimal("18000.00"), Decimal("4000.00"), Decimal("116000.00"))
    assert order.subtotal == Decimal("116000.00")
    assert order.total_amount == Decimal("116000.00")


def test_apply_lower_shrinks_the_courtesy(client, admin_auth_headers, courtesy_order, sample_product):
    _apply(client, admin_auth_headers, courtesy_order, _body(courtesy_order, sample_product, 1))

    item = Order.query.get(courtesy_order.id).order_items[0]
    assert (item.quantity, item.courtesy_quantity, item.courtesy_unit_price, item.discount_amount, item.total_price) == (
        1,
        1,
        Decimal("18000.00"),
        Decimal("2000.00"),
        Decimal("18000.00"),
    )


def test_edit_history_records_the_courtesy_split(client, admin_auth_headers, courtesy_order, sample_product):
    data = _apply(client, admin_auth_headers, courtesy_order, _body(courtesy_order, sample_product, 1))

    diff = OrderEditHistory.query.get(data["history_id"]).diff
    before, after = diff["items_before"][0], diff["items_after"][0]
    assert before["courtesy_quantity"] == 2
    assert before["courtesy_unit_price"] == 18000.0
    assert before["regular_quantity"] == 2
    assert after["courtesy_quantity"] == 1
    assert after["regular_quantity"] == 0
