"""Reward-flagging in order item serialization (additive-merge support)."""

from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product, ProductSizeEnum
from business_app.serializers.order_serializers import is_free_reward_item, serialize_order_item


@pytest.fixture
def product(db, sample_category):
    p = Product(name="19 litrlik suv", base_price=Decimal("18000"), category_id=sample_category.id,
                size=ProductSizeEnum.SIZE_19L, is_active=True)
    _db.session.add(p)
    _db.session.commit()
    return p


@pytest.mark.unit
def test_is_free_reward_item_true_only_for_zero_priced(product):
    paid = OrderItem(product_id=product.id, quantity=2,
                     unit_price=Decimal("18000.00"), total_price=Decimal("36000.00"))
    free = OrderItem(product_id=product.id, quantity=1,
                     unit_price=Decimal("0.00"), total_price=Decimal("0.00"))
    assert is_free_reward_item(paid) is False
    assert is_free_reward_item(free) is True


@pytest.mark.unit
def test_serialize_order_item_exposes_is_reward(db, sample_user, product):
    from shared.enums import OrderStatus
    order = Order(user_id=sample_user.id, order_number="ORD-RWD-1", status=OrderStatus.PENDING,
                  subtotal=Decimal("36000.00"), delivery_fee=Decimal("0.00"),
                  discount_amount=Decimal("0.00"), total_amount=Decimal("36000.00"))
    paid = OrderItem(order=order, product_id=product.id, quantity=2,
                     unit_price=Decimal("18000.00"), total_price=Decimal("36000.00"))
    free = OrderItem(order=order, product_id=product.id, quantity=1,
                     unit_price=Decimal("0.00"), total_price=Decimal("0.00"))
    _db.session.add(order)
    _db.session.commit()

    assert serialize_order_item(paid)["is_reward"] is False
    assert serialize_order_item(free)["is_reward"] is True
