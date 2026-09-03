"""The order payload must carry `tier_discount` as its own line.

Every client renders the discount as a labelled line of its own — the point of
the feature is that the customer sees their tier working — so the figure has to
reach them as a distinct field, not folded into `discount_amount` or `total`.
"""

from decimal import Decimal

from business_app.models.order import Order
from business_app.serializers.order_serializers import OrderSchema, serialize_order
from shared.enums import OrderStatus, PaymentMethod


def _order(db, user_id, tier_discount):
    order = Order(
        order_number=f"ORD-SER-{tier_discount}",
        user_id=user_id,
        status=OrderStatus.PENDING,
        subtotal=Decimal("30000.00"),
        discount_amount=Decimal("0.00"),
        delivery_fee=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        tier_discount=tier_discount,
        total_amount=Decimal("30000.00") - tier_discount,
        payment_method=PaymentMethod.CASH,
    )
    db.session.add(order)
    db.session.commit()
    return order


def test_serialize_order_emits_tier_discount(db, sample_user):
    payload = serialize_order(_order(db, sample_user.id, Decimal("2100.00")))

    assert payload["tier_discount"] == 2100.0
    assert payload["discount_amount"] == 0.0  # not folded into the subscription line
    assert payload["loyalty_discount"] == 0.0  # nor into the reward line


def test_serialize_order_emits_zero_for_an_undiscounted_order(db, sample_user):
    payload = serialize_order(_order(db, sample_user.id, Decimal("0.00")))

    assert payload["tier_discount"] == 0.0


def test_order_schema_declares_the_field_with_a_zero_default():
    assert "tier_discount" in OrderSchema.model_fields
    assert OrderSchema.model_fields["tier_discount"].default == 0
