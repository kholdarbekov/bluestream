"""view_order_details (the pool's order-detail view) renders the localized
delivery window instead of the raw legacy `time_slot` string (Task 13,
scheduled-delivery-orders).

Follows the same handler-testing idiom as
tests/staff_bot/test_active_delivery_detail_card.py: monkeypatch the
handler's auth helpers and the module-level `api_client`, drive the real
async handler, and assert on the text passed to `edit_message_text`.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.delivery import orders_pool as mod
from staff_bot.handlers.delivery.orders_pool import OrdersPoolHandler
from staff_bot.i18n import i18n


def _seed_window_translations(monkeypatch, language="en"):
    merged = {
        **i18n.translations.get(language, {}),
        "staff.delivery.window.anytime": "Anytime today",
        "staff.delivery.window.between": "Between {time}",
        "staff.delivery.window.until": "Deliver before {time}",
        "staff.delivery.window.after": "Deliver after {time}",
    }
    monkeypatch.setitem(i18n.translations, language, merged)


class _OrderPoolClient:
    def __init__(self, items):
        self.client = MagicMock()
        self.client.get_order_pool = AsyncMock(
            return_value=MagicMock(success=True, data={"items": items})
        )

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *a):
        return False


def _callback_update(data):
    cq = MagicMock()
    cq.answer = AsyncMock()
    cq.edit_message_text = AsyncMock()
    cq.data = data
    update = MagicMock()
    update.callback_query = cq
    update.message = None
    update.effective_user = MagicMock(id=777)
    return update, cq


def _context():
    ctx = MagicMock()
    ctx.user_data = {"language": "en", "authenticated": True,
                      "staff_roles": ["delivery_driver"]}
    ctx.bot = MagicMock()
    return ctx


_ORDER = {
    "order_id": 90, "delivery_id": 5, "order_number": "TG_1_26",
    "customer_name": "Umar", "customer_phone": "+998909150171",
    "district": "Chilanzar", "address": "Katta Qozirabot MFY",
    "items": [{"product_name": "19 litrlik suv", "quantity": 3}],
    "total_amount": 57000, "payment_method": "cash",
    "time_slot": "LEGACY_TIME_SLOT_TEXT",
    "delivery_window": {"start": "19:00", "end": None, "kind": "after", "label": "after 19:00"},
}


@pytest.mark.unit
def test_order_detail_renders_the_window_time(monkeypatch):
    _seed_window_translations(monkeypatch)
    handler = OrdersPoolHandler()
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(mod, "api_client", _OrderPoolClient([_ORDER]))
    update, cq = _callback_update("staff_view_order_5")
    ctx = _context()

    asyncio.run(handler.view_order_details(update, ctx))

    text = cq.edit_message_text.call_args.kwargs.get("text") or cq.edit_message_text.call_args.args[0]
    assert "19:00" in text
    assert "LEGACY_TIME_SLOT_TEXT" not in text


@pytest.mark.unit
def test_order_detail_never_renders_the_backend_label(monkeypatch):
    _seed_window_translations(monkeypatch)
    order = dict(_ORDER)
    order["delivery_window"] = {"start": None, "end": "10:00", "kind": "until",
                                 "label": "SENTINEL_ENGLISH_LABEL_MUST_NOT_LEAK"}
    handler = OrdersPoolHandler()
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(mod, "api_client", _OrderPoolClient([order]))
    update, cq = _callback_update("staff_view_order_5")
    ctx = _context()

    asyncio.run(handler.view_order_details(update, ctx))

    text = cq.edit_message_text.call_args.kwargs.get("text") or cq.edit_message_text.call_args.args[0]
    assert "SENTINEL_ENGLISH_LABEL_MUST_NOT_LEAK" not in text


@pytest.mark.unit
def test_order_detail_omits_anytime_window(monkeypatch):
    order = dict(_ORDER)
    order["delivery_window"] = {"start": None, "end": None, "kind": "anytime", "label": "anytime"}
    order.pop("time_slot", None)
    handler = OrdersPoolHandler()
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(mod, "api_client", _OrderPoolClient([order]))
    update, cq = _callback_update("staff_view_order_5")
    ctx = _context()

    asyncio.run(handler.view_order_details(update, ctx))

    text = cq.edit_message_text.call_args.kwargs.get("text") or cq.edit_message_text.call_args.args[0]
    assert "🕐" not in text
