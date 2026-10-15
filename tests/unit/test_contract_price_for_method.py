"""THE rule (spec 2026-10-09 §4.1): what one covered unit costs on one payment method."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from business_app.services.corporate_contract_service import contract_price_for_method
from shared.enums import PaymentMethod

CATALOGUE = Decimal("15000.00")
ROW = SimpleNamespace(business_account_unit_price=Decimal("12000.00"), cash_unit_price=Decimal("13500.00"))


@pytest.mark.unit
@pytest.mark.parametrize(
    "method, expected",
    [
        ("business_account", (Decimal("12000.00"), "contract_business_account")),
        (PaymentMethod.BUSINESS_ACCOUNT, (Decimal("12000.00"), "contract_business_account")),
        ("cash", (Decimal("13500.00"), "contract_cash")),
        (PaymentMethod.CASH, (Decimal("13500.00"), "contract_cash")),
        ("click", (CATALOGUE, "fallback")),
        ("card", (CATALOGUE, "fallback")),
        ("payme", (CATALOGUE, "fallback")),
        (None, (CATALOGUE, "fallback")),
        ("not-a-method", (CATALOGUE, "fallback")),
    ],
)
def test_each_method_pays_its_own_price(method, expected):
    assert contract_price_for_method(ROW, method, CATALOGUE) == expected


@pytest.mark.unit
@pytest.mark.parametrize("method, column", [("cash", "cash_unit_price"), ("business_account", "business_account_unit_price")])
def test_an_empty_price_pays_the_catalogue(method, column):
    row = SimpleNamespace(business_account_unit_price=Decimal("12000.00"), cash_unit_price=Decimal("13500.00"))
    setattr(row, column, None)
    assert contract_price_for_method(row, method, CATALOGUE) == (CATALOGUE, "fallback")


@pytest.mark.unit
def test_no_covering_row_pays_the_catalogue():
    assert contract_price_for_method(None, "cash", CATALOGUE) == (CATALOGUE, "fallback")


@pytest.mark.unit
@pytest.mark.parametrize("method", [["cash"], {"payment_method": "cash"}, 7], ids=["list", "dict", "int"])
def test_a_method_that_is_not_a_payment_method_pays_the_catalogue(method):
    """A raw JSON value reaches the rule unparsed (the operator's quote). `canonical_rail` hands an
    unhashable list or dict back unchanged; the rule must fall back, never raise."""
    assert contract_price_for_method(ROW, method, CATALOGUE) == (CATALOGUE, "fallback")
