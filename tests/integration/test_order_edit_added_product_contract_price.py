"""A line added in the admin order edit is priced the way the order prices it (spec 2026-10-09 §5.5)."""

from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.corporate import CorporateContractProductPrice
from business_app.models.order import Order
from business_app.models.product import Product
from tests.integration.contract_pricing_factory import (
    CASH,
    QUANTITY,
    admin_headers,
    customer_headers,
    make_contract,
    make_workplace,
)
from tests.integration.tier_discount_factory import post_order

pytestmark = pytest.mark.integration

SECOND_CASH = Decimal("17500.00")


@pytest.fixture
def world(db, sample_product, sample_category):
    user, address = make_workplace(db)
    contract, _row = make_contract(db, user, sample_product)
    second = Product(
        name="Water 19L Premium", description="d", category_id=sample_category.id, size="19L", volume=19.0,
        volume_unit="L", base_price=Decimal("20000.00"), stock_quantity=100, min_stock_level=1,
        max_stock_level=500, is_active=True,
    )
    db.session.add(second)
    db.session.flush()
    second_row = CorporateContractProductPrice(
        contract_id=contract.id, product_id=second.id, business_account_unit_price=Decimal("16000.00"),
        cash_unit_price=SECOND_CASH, is_prepayment_eligible=True, is_active=True,
    )
    db.session.add(second_row)
    db.session.commit()
    return user, address, second, second_row


def test_an_added_product_is_priced_at_the_contract_price_of_the_orders_method(
    app, db, admin_user, world, sample_product
):
    user, address, second, second_row = world
    resp = post_order(
        app, customer_headers(app, user), product_id=sample_product.id, address_id=address.id,
        payment_method="cash", quantity=QUANTITY,
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    order_id = resp.get_json()["data"]["order"]["id"]
    _db.session.expire_all()
    first_line = Order.query.get(order_id).order_items[0]

    resp = app.test_client().post(
        f"/api/v1/admin/orders/{order_id}/edit",
        json={
            "items": [
                {"order_item_id": first_line.id, "product_id": sample_product.id, "quantity": QUANTITY},
                {"product_id": second.id, "quantity": 2},
            ],
            "reason": "customer added a premium bottle",
        },
        headers=admin_headers(app, admin_user),
    )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    _db.session.expire_all()
    order = Order.query.get(order_id)
    added = next(item for item in order.order_items if item.product_id == second.id)
    assert Decimal(str(added.unit_price)) == SECOND_CASH
    assert added.contract_product_price_id == second_row.id
    assert Decimal(str(order.subtotal)) == CASH * QUANTITY + SECOND_CASH * 2
