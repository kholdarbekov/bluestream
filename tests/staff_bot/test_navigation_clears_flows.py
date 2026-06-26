"""Navigation landing handlers must drop any in-progress flow flags.

Backstop for the text-router state-leak class: whatever route a driver takes
back to a hub (reply-keyboard tap, inline Back, /start), a stale pending_*_flow
must not survive to mis-route the next text update.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Update

from staff_bot.handlers.delivery.status_update import StatusUpdateHandler


def _callback_ctx_update(flow_key, flow_value):
    cq = MagicMock()
    cq.answer = AsyncMock()
    cq.edit_message_text = AsyncMock()
    # spec=Update so the require_auth decorator's isinstance() branch treats this
    # as a real update (free-function handlers like main_menu_handler rely on it).
    update = MagicMock(spec=Update)
    update.callback_query = cq
    update.message = None
    update.effective_user = MagicMock(id=42)
    ctx = MagicMock()
    ctx.user_data = {
        "authenticated": True, "language": "en", "staff_roles": ["delivery_driver"],
        flow_key: flow_value,
    }
    ctx.bot = MagicMock()
    return ctx, update


@pytest.mark.unit
class TestNavigationClearsFlows:
    def test_main_menu_handler_clears_pending_flows(self, monkeypatch):
        from staff_bot.handlers import menu as menu_mod
        from staff_bot.utils import flow_state

        monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())
        ctx, update = _callback_ctx_update(
            "pending_cod_collection_flow", {"customer_id": 1, "amount": 5000}
        )

        asyncio.run(menu_mod.main_menu_handler(update, ctx))

        assert "pending_cod_collection_flow" not in ctx.user_data

    def test_show_cash_hub_clears_pending_flows(self, monkeypatch):
        from staff_bot.utils import flow_state

        monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())
        handler = StatusUpdateHandler.__new__(StatusUpdateHandler)
        handler._get_language = AsyncMock(return_value="en")
        ctx, update = _callback_ctx_update(
            "pending_delivery_cash_flow", {"delivery_id": 1, "flow_type": "partial"}
        )

        asyncio.run(handler.show_cash_hub(update, ctx))

        assert "pending_delivery_cash_flow" not in ctx.user_data

    def test_start_clears_pending_flows(self, monkeypatch):
        from staff_bot.handlers.start import StartHandler
        from staff_bot.utils import flow_state

        monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())
        handler = StartHandler.__new__(StartHandler)
        handler.user_repo = MagicMock()
        handler.user_repo.get_user_by_telegram_id = AsyncMock(
            return_value={"first_name": "Umar", "staff_roles": ["delivery_driver"]}
        )

        msg = MagicMock()
        msg.reply_text = AsyncMock()
        update = MagicMock()
        update.message = msg
        update.callback_query = None
        update.effective_user = MagicMock(id=42)
        ctx = MagicMock()
        ctx.user_data = {
            "authenticated": True, "language": "en", "staff_roles": ["delivery_driver"],
            "pending_bottle_collection_flow": {"customer_id": 1, "action": "collect", "quantity": 3},
        }
        ctx.bot = MagicMock()
        ctx.args = []

        asyncio.run(handler.start(update, ctx))

        assert "pending_bottle_collection_flow" not in ctx.user_data
