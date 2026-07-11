"""SSOT for payment methods: which exist, which a customer may pick, how
'card' normalizes. Pure module — no Flask app context, no DB."""

import pytest

from shared.enums import PaymentMethod
from shared.payment_methods import (
    CUSTOMER_SELECTABLE_METHODS,
    ORDER_PAYMENT_METHODS,
    PAYMENT_METHOD_CATALOG,
    READABLE_PAYMENT_METHODS,
    UnknownPaymentMethodError,
    UnsupportedPaymentMethodError,
    assert_customer_selectable,
    normalize_payment_method,
)


class TestNormalizePaymentMethod:
    def test_card_string_normalizes_to_click(self):
        assert normalize_payment_method("card") is PaymentMethod.CLICK

    def test_card_enum_normalizes_to_click(self):
        assert normalize_payment_method(PaymentMethod.CARD) is PaymentMethod.CLICK

    def test_known_strings_round_trip(self):
        assert normalize_payment_method("cash") is PaymentMethod.CASH
        assert normalize_payment_method("click") is PaymentMethod.CLICK
        assert normalize_payment_method("payme") is PaymentMethod.PAYME
        assert normalize_payment_method("business_account") is PaymentMethod.BUSINESS_ACCOUNT

    def test_enum_passthrough(self):
        assert normalize_payment_method(PaymentMethod.CASH) is PaymentMethod.CASH

    @pytest.mark.parametrize("bad", [None, "", "  ", "points", "bitcoin", "CASH ON DELIVERY"])
    def test_unknown_raises_instead_of_returning_none(self, bad):
        # Regression: order_service used payment_method_map.get(...) which
        # returned None and silently minted a NULL-payment_method order.
        with pytest.raises(UnknownPaymentMethodError):
            normalize_payment_method(bad)

    def test_loyalty_points_is_not_a_payment_method(self):
        with pytest.raises(UnknownPaymentMethodError):
            normalize_payment_method("loyalty_points")

    def test_loyalty_points_enum_raises_like_the_string_form(self):
        # Both input forms of the same value must behave identically.
        with pytest.raises(UnknownPaymentMethodError):
            normalize_payment_method(PaymentMethod.LOYALTY_POINTS)

    def test_payme_enum_is_readable_and_passes_through(self):
        assert normalize_payment_method(PaymentMethod.PAYME) is PaymentMethod.PAYME


class TestCustomerSelectable:
    def test_cash_click_business_account_are_selectable(self):
        for method in (PaymentMethod.CASH, PaymentMethod.CLICK, PaymentMethod.BUSINESS_ACCOUNT):
            assert_customer_selectable(method)  # must not raise

    def test_payme_is_not_selectable(self):
        with pytest.raises(UnsupportedPaymentMethodError):
            assert_customer_selectable(PaymentMethod.PAYME)

    def test_loyalty_points_is_not_selectable(self):
        with pytest.raises(UnsupportedPaymentMethodError):
            assert_customer_selectable(PaymentMethod.LOYALTY_POINTS)


class TestSets:
    def test_set_contents(self):
        assert ORDER_PAYMENT_METHODS == frozenset(
            {PaymentMethod.CASH, PaymentMethod.CLICK, PaymentMethod.PAYME, PaymentMethod.BUSINESS_ACCOUNT}
        )
        assert CUSTOMER_SELECTABLE_METHODS == frozenset(
            {PaymentMethod.CASH, PaymentMethod.CLICK, PaymentMethod.BUSINESS_ACCOUNT}
        )
        assert PaymentMethod.CARD in READABLE_PAYMENT_METHODS
        assert PaymentMethod.CARD not in ORDER_PAYMENT_METHODS

    def test_selectable_is_a_subset_of_order_methods(self):
        assert CUSTOMER_SELECTABLE_METHODS <= ORDER_PAYMENT_METHODS

    def test_loyalty_points_appears_in_no_set(self):
        for s in (ORDER_PAYMENT_METHODS, CUSTOMER_SELECTABLE_METHODS, READABLE_PAYMENT_METHODS):
            assert PaymentMethod.LOYALTY_POINTS not in s


class TestCatalog:
    def test_catalog_covers_exactly_the_non_business_account_selectable_methods(self):
        # business_account has no static catalog entry — it is appended by the
        # service only when the user is eligible.
        assert {entry["method"] for entry in PAYMENT_METHOD_CATALOG} == {"cash", "click"}

    def test_catalog_has_no_hardcoded_is_active(self):
        # is_active is derived from configured credentials by PaymentService.
        for entry in PAYMENT_METHOD_CATALOG:
            assert "is_active" not in entry

    def test_catalog_entries_have_display_metadata(self):
        for entry in PAYMENT_METHOD_CATALOG:
            assert entry["display_name"]
            assert entry["supported_currencies"] == ["UZS"]

    def test_catalog_entry_returns_a_deep_copy(self):
        from shared.payment_methods import catalog_entry

        entry = catalog_entry(PaymentMethod.CASH)
        entry["supported_currencies"].append("USD")
        assert catalog_entry(PaymentMethod.CASH)["supported_currencies"] == ["UZS"]


class TestRepeatablePaymentMethod:
    def test_payme_collapses_to_click(self):
        from shared.payment_methods import resolve_repeatable_payment_method

        assert resolve_repeatable_payment_method(PaymentMethod.PAYME) is PaymentMethod.CLICK
        assert resolve_repeatable_payment_method("payme") is PaymentMethod.CLICK

    def test_card_collapses_to_click(self):
        from shared.payment_methods import resolve_repeatable_payment_method

        assert resolve_repeatable_payment_method(PaymentMethod.CARD) is PaymentMethod.CLICK

    def test_selectable_methods_pass_through(self):
        from shared.payment_methods import resolve_repeatable_payment_method

        for method in (PaymentMethod.CASH, PaymentMethod.CLICK, PaymentMethod.BUSINESS_ACCOUNT):
            assert resolve_repeatable_payment_method(method) is method

    def test_null_raises_unknown(self):
        from shared.payment_methods import UnknownPaymentMethodError, resolve_repeatable_payment_method

        with pytest.raises(UnknownPaymentMethodError):
            resolve_repeatable_payment_method(None)

    def test_loyalty_points_raises_unknown(self):
        from shared.payment_methods import UnknownPaymentMethodError, resolve_repeatable_payment_method

        with pytest.raises(UnknownPaymentMethodError):
            resolve_repeatable_payment_method(PaymentMethod.LOYALTY_POINTS)


def test_shared_module_does_not_import_business_app():
    import pathlib

    source = pathlib.Path("shared/payment_methods.py").read_text()
    assert "business_app" not in source
    assert "import flask" not in source
    assert "from flask" not in source
