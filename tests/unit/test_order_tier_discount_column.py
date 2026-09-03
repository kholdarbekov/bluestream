"""orders.tier_discount — the column a COD loyalty-tier discount is recorded in.

Deliberately NOT a reuse of an existing column:

  * ``discount_amount`` means *subscription percentage*. It is set from the
    Subscription row so a caller cannot forge it, and it is invisible to the
    Click fiscal receipt builder.
  * ``loyalty_discount`` means *redeemed reward*, and IS consumed by
    ``PaymentFiscalizationService.build_click_fiscalization_payload``'s
    per-item ``Discount`` allocator.

All three can legitimately sit on one order, so overloading either would give
a column two meanings and an unrecoverable audit trail.
"""

from decimal import Decimal

from business_app.models.order import Order, OrderItem
from business_app.utils.order_totals import compute_order_total
from shared.enums import OrderStatus, PaymentMethod


def test_new_order_defaults_tier_discount_to_zero(db, sample_user):
    order = Order(
        order_number="ORD-TIER-DEFAULT",
        user_id=sample_user.id,
        status=OrderStatus.PENDING,
        subtotal=Decimal("30000.00"),
        delivery_fee=Decimal("0.00"),
        total_amount=Decimal("30000.00"),
        payment_method=PaymentMethod.CASH,
    )
    db.session.add(order)
    db.session.commit()

    assert Decimal(str(order.tier_discount)) == Decimal("0.00")


def test_calculate_total_subtracts_tier_discount(db, sample_user, sample_product):
    """``Order.calculate_total`` is the declared SSOT for the formula; it must
    carry the sixth term or the quoted total disagrees with the charged one."""
    order = Order(
        order_number="ORD-TIER-TOTAL",
        user_id=sample_user.id,
        status=OrderStatus.PENDING,
        subtotal=Decimal("0.00"),
        discount_amount=Decimal("1000.00"),
        delivery_fee=Decimal("2000.00"),
        loyalty_discount=Decimal("500.00"),
        tier_discount=Decimal("2100.00"),
        total_amount=Decimal("0.00"),
        payment_method=PaymentMethod.CASH,
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=sample_product.id,
            quantity=2,
            unit_price=Decimal("15000.00"),
            total_price=Decimal("30000.00"),
        )
    )
    db.session.commit()

    assert order.calculate_total() == compute_order_total(
        subtotal=Decimal("30000.00"),
        discount_amount=Decimal("1000.00"),
        delivery_fee=Decimal("2000.00"),
        loyalty_discount=Decimal("500.00"),
        tier_discount=Decimal("2100.00"),
    )
    assert order.total_amount == Decimal("28400.00")
