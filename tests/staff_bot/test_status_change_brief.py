"""Status-change confirm/updated messages carry a short order brief so drivers
see which order they're updating."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.delivery import status_update as mod
from staff_bot.handlers.delivery.status_update import StatusUpdateHandler


class _UpdateStatusClient:
    def __init__(self, response):
        self.client = MagicMock()
        self.client.update_delivery_status = AsyncMock(return_value=response)

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


_SNAPSHOT = {
    "delivery_id": 5, "order_number": "AD_000028_26", "status": "assigned",
    "customer_name": "Umar", "customer_phone": "+998909150171",
    "district": "Chilanzar", "address": "Katta Qozirabot MFY",
    "items": [{"product_name": "19 litrlik suv", "quantity": 3}],
    "payment_method": "cash", "total_amount": 57000,
    "expected_cash_to_collect": 57000,
}


def _context(current_delivery):
    ctx = MagicMock()
    ctx.user_data = {"language": "uz", "authenticated": True,
                     "staff_roles": ["delivery_driver"]}
    if current_delivery is not None:
        ctx.user_data["current_delivery"] = current_delivery
    ctx.bot = MagicMock()
    return ctx


@pytest.mark.unit
class TestOrderBriefHelper:
    def test_returns_brief_with_name_items_no_money(self):
        ctx = _context(dict(_SNAPSHOT))
        brief = StatusUpdateHandler._order_brief(ctx, "uz")
        assert "👤 Umar" in brief
        assert "📦 19 litrlik suv ×3" in brief
        assert "💰" not in brief and "💵" not in brief
        assert brief.endswith("\n\n")

    def test_returns_empty_when_no_snapshot(self):
        ctx = _context(None)
        assert StatusUpdateHandler._order_brief(ctx, "uz") == ""


@pytest.mark.unit
class TestGenericConfirmPrependsBrief:
    def test_confirm_message_has_brief(self, monkeypatch):
        handler = StatusUpdateHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="uz"))
        update, cq = _callback_update("staff_status_5_in_transit")
        ctx = _context(dict(_SNAPSHOT))

        asyncio.run(handler.initiate_status_change(update, ctx))

        text = cq.edit_message_text.call_args.args[0]
        assert "👤 Umar" in text and "📦 19 litrlik suv ×3" in text


@pytest.mark.unit
class TestGenericUpdatedPrependsBrief:
    def test_status_updated_message_has_brief(self, monkeypatch):
        handler = StatusUpdateHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="uz"))
        monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
        monkeypatch.setattr(mod, "api_client",
                            _UpdateStatusClient(MagicMock(success=True)))
        update, cq = _callback_update("staff_execute_status_5_in_transit")
        ctx = _context(dict(_SNAPSHOT))

        asyncio.run(handler.execute_status_change(update, ctx))

        text = cq.edit_message_text.call_args.args[0]
        assert "👤 Umar" in text and "📦 19 litrlik suv ×3" in text
        assert "✅" in text


@pytest.mark.unit
class TestFailedPathsPrependBrief:
    def test_failed_reason_prompt_has_brief(self, monkeypatch):
        handler = StatusUpdateHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="uz"))
        update, cq = _callback_update("staff_status_5_failed")
        ctx = _context(dict(_SNAPSHOT))

        asyncio.run(handler.initiate_status_change(update, ctx))

        text = cq.edit_message_text.call_args.args[0]
        assert "👤 Umar" in text and "📦 19 litrlik suv ×3" in text

    def test_failed_success_message_has_brief(self, monkeypatch):
        handler = StatusUpdateHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="uz"))
        monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
        monkeypatch.setattr(mod, "api_client",
                            _UpdateStatusClient(MagicMock(success=True)))
        update, cq = _callback_update("staff_failed_reason_5_no_answer")
        ctx = _context(dict(_SNAPSHOT))

        asyncio.run(handler.select_fail_reason(update, ctx))

        text = cq.edit_message_text.call_args.args[0]
        assert "👤 Umar" in text and "📦 19 litrlik suv ×3" in text
