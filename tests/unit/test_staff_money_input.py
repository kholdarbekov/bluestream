"""The one rule for every amount a driver types (Task 20)."""
from decimal import Decimal

import pytest

from staff_bot.utils.money_input import parse_typed_money


@pytest.mark.parametrize("text,value", [
    ("150000", "150000"),
    ("150 000", "150000"),
    ("150 000", "150000"),
    ("150 000", "150000"),
    ("150,000", "150000"),
    ("150.000", "150000"),
    ("1.500.000", "1500000"),
    ("150 000,00", "150000.00"),
    ("1,500,000.50", "1500000.50"),
    ("150.5", "150.5"),
    ("150,50", "150.50"),
    ("150,5", "150.5"),
    ("0", "0"),
])
def test_accepted(text, value):
    assert parse_typed_money(text) == Decimal(value)


@pytest.mark.parametrize("text", [
    "1500.000", "1.500.000.50", "1,500.000", "1 500,000", "1,50,000",
    "-5000", "+5", "nan", "inf", "Infinity", "1e5", "", "12a", "1" * 400,
])
def test_refused(text):
    with pytest.raises(ValueError):
        parse_typed_money(text)


def test_none_is_refused():
    with pytest.raises(ValueError):
        parse_typed_money(None)
