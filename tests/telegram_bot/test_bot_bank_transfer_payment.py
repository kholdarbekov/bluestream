"""TDD tests for business_account payment method rendering and handler extraction."""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import eligibility
import handlers.orders as orders_module
from handlers.orders import OrderHandlers
from tests.telegram_bot.helpers import DummyCallbackQuery, DummyUpdate, FakeAPIClientContext, make_context


def _resp(success=True, data=None, error=None, status_code=200):
    return SimpleNamespace(success=success, data=data or {}, error=error, status_code=status_code)


def _i18n_get(key, language, *args, **kwargs):
    return f"{key}:{language}"


def _types(methods):
    return [m['type'] for m in methods]


def test_business_account_gets_its_own_button():
    methods = OrderHandlers._build_checkout_payment_methods(
        [{'method': 'cash'}, {'method': 'click'}, {'method': 'business_account'}], 'en')
    assert 'business_account' in _types(methods)
    assert 'cash' in _types(methods)
    assert 'card' in _types(methods)


def test_business_account_only_does_not_add_phantom_card():
    methods = OrderHandlers._build_checkout_payment_methods(
        [{'method': 'business_account'}], 'en')
    assert _types(methods) == ['business_account']  # no spurious 'card'


def test_cash_and_card_unchanged_when_no_business_account():
    methods = OrderHandlers._build_checkout_payment_methods(
        [{'method': 'cash'}, {'method': 'click'}], 'en')
    assert _types(methods) == ['cash', 'card']


@pytest.mark.anyio
async def test_payment_handler_extracts_business_account(monkeypatch):
    import handlers.orders as orders_mod

    handler = OrderHandlers()
    monkeypatch.setattr(handler, "_show_order_confirmation", AsyncMock())
    monkeypatch.setattr(orders_mod.i18n, "get_user_language", AsyncMock(return_value="en"))

    class _Query:
        data = "payment_business_account"
        async def answer(self, *a, **k): pass

    class _User:
        id = 123

    class _Update:
        callback_query = _Query()
        effective_user = _User()

    ctx = type("Ctx", (), {"user_data": {}})()
    await handler.payment_handler(_Update(), ctx)
    assert ctx.user_data['selected_payment_method'] == 'business_account'


@pytest.mark.unit
@pytest.mark.anyio
async def test_show_order_confirmation_business_account_label(monkeypatch):
    """Confirm screen renders 'Bank Transfer' label, not 'Unknown', for business_account."""
    handler = OrderHandlers()
    update = DummyUpdate()
    update.callback_query = DummyCallbackQuery(data="payment_business_account")
    context = make_context()
    context.user_data["selected_address_id"] = 5
    context.user_data["selected_payment_method"] = "business_account"

    # `total_price` / `subtotal` are what `CartService.get_cart_details` serves
    # and what the confirmation screen reads; a cart literal without them
    # exercises a zero total. See
    # tests/integration/test_checkout_total_is_server_authoritative.py.
    cart_data = {
        "data": {
            "cart": {
                "cart_items": [
                    {
                        "product": {"id": 1, "name": "Bottle", "current_price": 12000},
                        "quantity": 1,
                        "total_price": 12000,
                    },
                ],
                "subtotal": 12000,
            }
        }
    }
    estimate_data = {
        "data": {
            "items": [
                {"product_id": 1, "product_name": "Bottle", "quantity": 1,
                 "unit_price": 12000, "subtotal": 12000},
            ],
            "pricing": {
                "items_subtotal": 12000.0, "delivery_fee": 0.0,
                "discount_amount": 0.0, "loyalty_discount": 0.0,
                "tier_discount": 0.0, "tier_name": None,
                "tier_discount_percentage": 0.0, "cod_savings": 0.0,
                "payment_method": "business_account", "final_total": 12000.0,
            },
        }
    }
    monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=False))
    monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
    monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
    monkeypatch.setattr(orders_module.OrderKeyboards, "order_confirmation", lambda *_a, **_k: "confirm-kbd")
    monkeypatch.setattr(orders_module, "api_client", FakeAPIClientContext(
        get_cart=_resp(success=True, data=cart_data),
        estimate_cart=_resp(success=True, data=estimate_data),
    ))

    await handler._show_order_confirmation(update, context)

    text = update.callback_query.edit_message_text.await_args.kwargs["text"]
    # The i18n stub returns "key:language", so business_account label renders as:
    assert "telegram.payment_business_account:en" in text
    # And the unknown fallback must NOT appear
    assert "telegram.common.unknown:en" not in text
