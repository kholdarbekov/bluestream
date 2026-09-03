"""THE ORDER TOTAL FORMULA, IN ONE PLACE.

Before this module the expression

    subtotal - discount_amount + delivery_fee - loyalty_discount

was re-typed in TEN production sites (design spec 2026-08-27 §4.6), and four of
them had already drifted:

  * `OrderService.create_order` omitted `loyalty_discount` entirely.
  * `StaffService.price_phone_order` carried NO discount term at all.
  * `OrderEditService._project_totals_after` (the edit PREVIEW) hardcoded
    `loyalty_discount: 0.0` while `_recompute_totals` (the APPLY) read the
    column — a preview that understated the discount it was about to write.
  * `order_tasks.validate_order_integrity` computed a loyalty-blind total AND
    COMMITTED IT BACK to the row. It was disarmed only by having no caller.

Adding a term is now one edit, not ten, and a quoted total cannot disagree with
a charged one.

NO QUANTIZATION HAPPENS HERE. Every operand is expected to arrive already
rounded to 2dp by whoever derived it (`order_service.py:168` quantizes the
subscription discount with ROUND_HALF_UP before it reaches this function). A
sum of 2dp Decimals is exact, so rounding here would be a no-op on real inputs
and a silent behaviour change on any caller that has not quantized yet.

NO CLAMPING HAPPENS HERE either. Whether a total may reach or pass zero is a
policy owned by the CALLER — `create_order` rejects it (order_service.py:179),
`apply_reward_to_order` floors it at zero — and burying a clamp in the formula
would stop `create_order`'s rejection from ever firing.
"""

from decimal import Decimal


def compute_order_total(
    *,
    subtotal: Decimal,
    discount_amount: Decimal,
    delivery_fee: Decimal,
    loyalty_discount: Decimal,
    tier_discount: Decimal,
) -> Decimal:
    """The order total. All five arguments are Decimal and all are required.

    Args:
        subtotal: sum of ``OrderItem.total_price``.
        discount_amount: the SUBSCRIPTION percentage discount.
        delivery_fee: the delivery charge.
        loyalty_discount: a REDEEMED REWARD's discount.
        tier_discount: the loyalty-tier COD discount. Callers that cannot yet
            produce one pass ``Decimal("0.00")``.

    Keyword-only on purpose: five same-typed money arguments in a row are
    exactly the shape that gets silently transposed at a call site, and
    ``delivery_fee`` and ``loyalty_discount`` carry opposite signs.
    """
    return subtotal - discount_amount + delivery_fee - loyalty_discount - tier_discount
