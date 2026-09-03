"""Unit pins for the ONE order-total formula (design spec 2026-08-27 §4.6)."""

from decimal import Decimal

import pytest

from business_app.utils.order_totals import compute_order_total


@pytest.mark.unit
def test_all_five_terms_carry_their_sign():
    """The two `+`-signed and three `-`-signed operands, in one assertion.

    36000 - 1000 + 3000 - 500 - 720 = 36780.
    """
    assert compute_order_total(
        subtotal=Decimal("36000.00"),
        discount_amount=Decimal("1000.00"),
        delivery_fee=Decimal("3000.00"),
        loyalty_discount=Decimal("500.00"),
        tier_discount=Decimal("720.00"),
    ) == Decimal("36780.00")


@pytest.mark.unit
def test_the_three_discounts_stack_additively():
    """Spec §4.4: subscription, reward and tier all reduce the SAME total."""
    base = {
        "subtotal": Decimal("36000.00"),
        "delivery_fee": Decimal("0.00"),
        "discount_amount": Decimal("0.00"),
        "loyalty_discount": Decimal("0.00"),
        "tier_discount": Decimal("0.00"),
    }
    none_applied = compute_order_total(**base)
    all_applied = compute_order_total(
        **{
            **base,
            "discount_amount": Decimal("1000.00"),
            "loyalty_discount": Decimal("500.00"),
            "tier_discount": Decimal("720.00"),
        }
    )
    assert none_applied - all_applied == Decimal("2220.00")


@pytest.mark.unit
def test_it_returns_decimal_and_does_not_round():
    """Rounding is the CALLER's job — see the module docstring.

    Every operand arrives already quantized by whoever derived it. Rounding
    here would be a no-op on real inputs and a silent behaviour change on any
    caller that has not quantized yet.
    """
    result = compute_order_total(
        subtotal=Decimal("100.005"),
        discount_amount=Decimal("0.00"),
        delivery_fee=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        tier_discount=Decimal("0.00"),
    )
    assert isinstance(result, Decimal)
    assert result == Decimal("100.005")


@pytest.mark.unit
def test_it_does_not_clamp_a_negative_total():
    """`create_order` REJECTS `total_amount <= 0` (order_service.py:179).

    If the formula floored at zero that rejection could never fire and a
    free order would persist. Clamping is a per-call-site policy.
    """
    assert compute_order_total(
        subtotal=Decimal("1000.00"),
        discount_amount=Decimal("900.00"),
        delivery_fee=Decimal("0.00"),
        loyalty_discount=Decimal("200.00"),
        tier_discount=Decimal("0.00"),
    ) == Decimal("-100.00")


@pytest.mark.unit
def test_every_argument_is_keyword_only():
    """Five same-typed money arguments positionally is a transposition waiting
    to happen — `delivery_fee` and `loyalty_discount` carry OPPOSITE signs."""
    with pytest.raises(TypeError):
        compute_order_total(
            Decimal("1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")
        )
