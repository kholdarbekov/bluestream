"""COD collection flow-state hardening: every typed-input screen must carry an
inline Cancel, and terminal error exits must drop the routing flag so a stale
flow can't poison the next text update.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.delivery.cash_collection import CashCollectionHandler


class _RecordApiClient:
    """Async-context-manager exposing record_cash_collection + statement."""

    def __init__(self, record_response, statement_response=None):
        self.client = MagicMock()
        self.client.record_cash_collection = AsyncMock(return_value=record_response)
        self.client.get_customer_cod_statement = AsyncMock(
            return_value=statement_response
            or MagicMock(success=True, data={"total_outstanding_amount": 0})
        )

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _callback_update(callback_data):
    cq = MagicMock()
    cq.answer = AsyncMock()
    cq.edit_message_text = AsyncMock()
    cq.data = callback_data
    update = MagicMock()
    update.callback_query = cq
    update.message = None
    update.effective_user = MagicMock(id=777)
    return update, cq


def _message_update(text):
    msg = MagicMock()
    msg.text = text
    msg.reply_text = AsyncMock()
    update = MagicMock()
    update.callback_query = None
    update.message = msg
    update.effective_user = MagicMock(id=777)
    return update, msg


def _context(flow):
    ctx = MagicMock()
    ctx.user_data = {"language": "en", "authenticated": True,
                     "staff_roles": ["delivery_driver"], "pending_cod_collection_flow": flow}
    ctx.bot = MagicMock()
    return ctx


def _markup_callbacks(call):
    markup = call.kwargs.get("reply_markup")
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.mark.unit
class TestOverpaymentRepromptsHaveCancel:
    def test_confirm_overpayment_note_prompt_has_cancel_button(self, monkeypatch):
        handler = CashCollectionHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
        update, cq = _callback_update("staff_cod_confirm_overpay_yes")
        ctx = _context({"customer_id": 42, "pending_overpayment_amount": 150000.0,
                        "total_outstanding_amount": 100000.0})

        asyncio.run(handler.confirm_overpayment_collection(update, ctx))

        assert "staff_flow_cancel" in _markup_callbacks(cq.edit_message_text.call_args)

    def test_cancel_overpayment_amount_prompt_has_cancel_button(self, monkeypatch):
        handler = CashCollectionHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
        update, cq = _callback_update("staff_cod_confirm_overpay_no")
        ctx = _context({"customer_id": 42, "pending_overpayment_amount": 150000.0,
                        "total_outstanding_amount": 100000.0})

        asyncio.run(handler.cancel_overpayment_collection(update, ctx))

        assert "staff_flow_cancel" in _markup_callbacks(cq.edit_message_text.call_args)


@pytest.mark.unit
class TestFailedRecordClearsFlow:
    def test_failed_cash_record_clears_pending_cod_flow(self, monkeypatch):
        """If record_cash_collection fails, the flow must be cleared so the next
        typed text isn't re-submitted as another collection note."""
        from staff_bot.handlers.delivery import cash_collection as mod
        from staff_bot.utils import flow_state

        handler = CashCollectionHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="token"))
        monkeypatch.setattr(handler, "_handle_api_response_error", AsyncMock())
        monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())
        monkeypatch.setattr(
            mod, "api_client",
            _RecordApiClient(MagicMock(success=False, status_code=500, error="boom")),
        )

        update, _ = _message_update("paid in cash")
        ctx = _context({"customer_id": 42, "amount": 50000.0})

        asyncio.run(handler.receive_collection_note(update, ctx))

        assert not ctx.user_data.get("pending_cod_collection_flow")
