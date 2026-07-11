from unittest.mock import patch

import pytest

from business_app.services.payment_service import PaymentContext, PaymentService


def _methods(result):
    return {entry["method"] for entry in result}


class TestAvailablePaymentMethods:
    def test_payme_is_never_offered(self, app, db, sample_user):
        with app.app_context():
            result = PaymentService().get_available_payment_methods(sample_user)
            assert "payme" not in _methods(result)

    def test_loyalty_points_is_never_offered(self, app, db, sample_user):
        with app.app_context():
            result = PaymentService().get_available_payment_methods(sample_user)
            assert "loyalty_points" not in _methods(result)

    def test_individual_gets_cash_and_click_only(self, app, db, sample_user):
        with app.app_context():
            result = PaymentService().get_available_payment_methods(sample_user)
            assert _methods(result) == {"cash", "click"}

    def test_cash_is_withheld_when_cod_restricted(self, app, db, sample_user):
        with app.app_context(), patch(
            "business_app.services.cash_collection_service.CashCollectionService.get_cod_restriction_context",
            return_value={"cod_restricted": True},
        ):
            result = PaymentService().get_available_payment_methods(sample_user)
            assert "cash" not in _methods(result)

    def test_click_is_hidden_when_unconfigured(self, app, db, sample_user, monkeypatch):
        # `app` is session-scoped (tests/conftest.py) — mutate via monkeypatch
        # so the override is undone at teardown instead of poisoning every
        # other test in this session with a permanently-unconfigured Click.
        monkeypatch.setitem(app.config, "CLICK_MERCHANT_ID", None)
        monkeypatch.setitem(app.config, "CLICK_SERVICE_ID", None)
        with app.app_context():
            result = PaymentService().get_available_payment_methods(sample_user)
            assert "click" not in _methods(result)

    def test_business_account_offered_only_to_eligible_workplace(self, app, db, sample_user):
        with app.app_context(), patch(
            "business_app.services.corporate_contract_service.CorporateContractService.user_can_use_business_account",
            return_value=True,
        ):
            result = PaymentService().get_available_payment_methods(
                sample_user, context=PaymentContext.SUBSCRIPTION
            )
            assert "business_account" in _methods(result)

    def test_order_context_with_items_uses_the_item_level_mirror(self, app, db, sample_user, sample_product):
        items = [{"product_id": sample_product.id, "quantity": 1}]
        with app.app_context(), patch(
            "business_app.services.corporate_contract_service.CorporateContractService."
            "order_qualifies_for_business_account",
            return_value=True,
        ) as mirror:
            result = PaymentService().get_available_payment_methods(
                sample_user, context=PaymentContext.ORDER, items=items
            )
            mirror.assert_called_once()
            assert "business_account" in _methods(result)
