from decimal import Decimal

import pytest

from business_app.models.order import OrderItem


@pytest.mark.unit
def test_order_item_is_reward_item_defaults_false(db, sample_order, sample_product):
    item = OrderItem(
        order_id=sample_order.id,
        product_id=sample_product.id,
        quantity=1,
        unit_price=Decimal("1000.00"),
        total_price=Decimal("1000.00"),
    )
    db.session.add(item)
    db.session.commit()
    db.session.refresh(item)
    assert item.is_reward_item is False


@pytest.mark.unit
def test_order_item_is_reward_item_can_be_set_true(db, sample_order, sample_product):
    item = OrderItem(
        order_id=sample_order.id,
        product_id=sample_product.id,
        quantity=1,
        unit_price=Decimal("0.00"),
        total_price=Decimal("0.00"),
        is_reward_item=True,
    )
    db.session.add(item)
    db.session.commit()
    db.session.refresh(item)
    assert item.is_reward_item is True
