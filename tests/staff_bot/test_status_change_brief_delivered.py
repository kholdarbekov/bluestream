"""The delivered flow's confirm + completion messages also carry the brief."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.delivery import status_update as mod
from staff_bot.handlers.delivery.status_update import StatusUpdateHandler
from staff_bot.utils import flow_state


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
    "delivery_id": 5, "order_number": "AD_000028_26", "status": "arrived",
    "customer_name": "Umar", "customer_phone": "+998909150171",
    "district": "Chilanzar", "address": "Katta Qozirabot MFY",
    "items": [{"product_name": "19 litrlik suv", "quantity": 3}],
    "payment_method": "cash", "payment_status": "pending", "total_amount": 57000,
    "amount_collected": 0, "outstanding_amount": 57000,
    "expected_cash_to_collect": 57000, "cod_reserved_prepayment_amount": 0,
    "expected_returnable_bottles": 0, "customer_bottle_balance": 0,
}


def _context(current_delivery, extra=None):
    ctx = MagicMock()
    ctx.user_data = {"language": "uz", "authenticated": True,
                     "staff_roles": ["delivery_driver"],
                     "current_delivery": current_delivery}
    if extra:
        ctx.user_data.update(extra)
    ctx.bot = MagicMock()
    return ctx


@pytest.mark.unit
class TestDeliveredFlowBriefs:
    def test_cash_collection_prompt_has_brief(self, monkeypatch):
        handler = StatusUpdateHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="uz"))
        update, cq = _callback_update("staff_status_5_delivered")
        ctx = _context(dict(_SNAPSHOT))

        asyncio.run(handler.initiate_status_change(update, ctx))

        text = cq.edit_message_text.call_args.args[0]
        assert "👤 Umar" in text and "📦 19 litrlik suv ×3" in text

    def test_bottle_prompt_has_brief(self, monkeypatch):
        handler = StatusUpdateHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="uz"))
        monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
        update, cq = _callback_update("staff_execute_status_5_delivered")
        snap = dict(_SNAPSHOT)
        snap["expected_returnable_bottles"] = 2
        snap["customer_bottle_balance"] = 2
        ctx = _context(snap)

        asyncio.run(handler.execute_status_change(update, ctx))

        text = cq.edit_message_text.call_args.args[0]
        assert "👤 Umar" in text and "📦 19 litrlik suv ×3" in text

    def test_cash_flow_bottle_prompt_has_brief(self, monkeypatch):
        """The OTHER bottle-return prompt render site (cash-collection ->
        bottle path via `_maybe_show_bottle_prompt_or_submit`) must also
        carry the brief, mirroring the non-cash `execute_status_change` site."""
        handler = StatusUpdateHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="uz"))
        update, cq = _callback_update("staff_cash_5_57000")
        snap = dict(_SNAPSHOT)
        snap["expected_returnable_bottles"] = 2
        snap["customer_bottle_balance"] = 2
        ctx = _context(snap)

        asyncio.run(handler._maybe_show_bottle_prompt_or_submit(
            update, ctx, delivery_id=5, cash_amount=57000))

        text = cq.edit_message_text.call_args.args[0]
        assert "👤 Umar" in text and "📦 19 litrlik suv ×3" in text

    def test_delivered_success_has_brief(self, monkeypatch):
        handler = StatusUpdateHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="uz"))
        monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
        monkeypatch.setattr(mod, "api_client",
                            _UpdateStatusClient(MagicMock(success=True)))
        monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())
        update, cq = _callback_update("noop")
        ctx = _context(dict(_SNAPSHOT))

        asyncio.run(handler._submit_delivery_completion(
            update, ctx, delivery_id=5, cash_amount=57000))

        text = cq.edit_message_text.call_args.args[0]
        assert "👤 Umar" in text and "📦 19 litrlik suv ×3" in text
        assert "✅" in text
