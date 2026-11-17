"""The one line-money formula (spec 2026-11-16 section 4.3 and 5.7)."""

from decimal import Decimal

import pytest

from business_app.models.order import OrderItem
from business_app.utils.line_money import LineMoney, line_money


def test_no_courtesy_is_unit_times_quantity():
    assert line_money(Decimal("15000.00"), 3) == LineMoney(Decimal("0.00"), Decimal("45000.00"), None)


def test_the_spec_mixed_order():  # spec 4.3: 4 bottles, cap 2, 18,000 -> 20,000
    assert line_money(Decimal("20000.00"), 4, 2, Decimal("18000.00")) == LineMoney(
        Decimal("4000.00"), Decimal("76000.00"), 2
    )


def test_every_unit_at_the_old_price():
    assert line_money(Decimal("20000.00"), 2, 2, Decimal("18000.00")) == LineMoney(
        Decimal("4000.00"), Decimal("36000.00"), 0
    )


def test_both_figures_are_tiyin_exact():
    mixed = line_money(Decimal("20000.00"), 4, 2, Decimal("18000.00"))
    plain = line_money(Decimal("15000.00"), 3)
    assert mixed.discount_amount.as_tuple().exponent == -2
    assert mixed.total_price.as_tuple().exponent == -2
    assert plain.discount_amount.as_tuple().exponent == -2
    assert plain.total_price.as_tuple().exponent == -2


@pytest.mark.parametrize(
    "courtesy_quantity, courtesy_unit_price",
    [
        (2, None),
        (None, Decimal("18000.00")),
        (5, Decimal("18000.00")),
        (-1, Decimal("18000.00")),
        (0, Decimal("18000.00")),
        (2, Decimal("21000.00")),
    ],
)
def test_an_impossible_courtesy_is_refused(courtesy_quantity, courtesy_unit_price):
    with pytest.raises(ValueError):
        line_money(Decimal("20000.00"), 4, courtesy_quantity, courtesy_unit_price)


def test_the_order_line_keeps_no_second_formula():
    assert not hasattr(OrderItem, "calculate_total")
