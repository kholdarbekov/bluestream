"""Runtime checks for the COD overpayment confirmation handler logic."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from staff_bot.handlers.delivery.cash_collection import (
    COLLECTION_AMOUNT_INPUT,
    COLLECTION_NOTE_INPUT,
    CashCollectionHandler,
)


def _make_update_with_callback(callback_data: str):
    callback_query = MagicMock()
    callback_query.answer = AsyncMock()
    callback_query.edit_message_text = AsyncMock()
    callback_query.data = callback_data
    update = MagicMock()
    update.callback_query = callback_query
    update.effective_user = MagicMock(id=12345)
    return update, callback_query


def _make_context(flow: dict, language: str = "en"):
    context = MagicMock()
    context.user_data = {
        "language": language,
        "authenticated": True,
        "staff_roles": ["delivery_driver"],
        "pending_cod_collection_flow": flow,
    }
    return context


def test_confirm_overpayment_moves_pending_amount_to_amount(monkeypatch):
    handler = CashCollectionHandler()

    update, callback_query = _make_update_with_callback("staff_cod_confirm_overpay_yes")
    flow = {
        "customer_id": 42,
        "pending_overpayment_amount": 150_000.0,
        "total_outstanding_amount": 100_000.0,
    }
    context = _make_context(flow)

    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))

    state = asyncio.run(handler.confirm_overpayment_collection(update, context))

    assert state == COLLECTION_NOTE_INPUT
    saved_flow = context.user_data["pending_cod_collection_flow"]
    assert saved_flow["amount"] == 150_000.0
    assert "pending_overpayment_amount" not in saved_flow
    callback_query.edit_message_text.assert_awaited()


def test_cancel_overpayment_resets_amount_and_returns_to_amount_input(monkeypatch):
    handler = CashCollectionHandler()

    update, callback_query = _make_update_with_callback("staff_cod_confirm_overpay_no")
    flow = {
        "customer_id": 42,
        "pending_overpayment_amount": 150_000.0,
        "total_outstanding_amount": 100_000.0,
    }
    context = _make_context(flow)

    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))

    state = asyncio.run(handler.cancel_overpayment_collection(update, context))

    assert state == COLLECTION_AMOUNT_INPUT
    saved_flow = context.user_data["pending_cod_collection_flow"]
    assert "amount" not in saved_flow
    assert "pending_overpayment_amount" not in saved_flow
    callback_query.edit_message_text.assert_awaited()
