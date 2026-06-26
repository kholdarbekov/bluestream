"""Status-update flow-state hardening: arming a typed-input flow must roll back
if the prompt fails to render, so no flag is left without a UI to drive it."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.delivery.status_update import StatusUpdateHandler


def _handler():
    h = StatusUpdateHandler.__new__(StatusUpdateHandler)
    h._get_language = AsyncMock(return_value="en")
    return h


def _callback_update(render_side_effect=None):
    cq = MagicMock()
    cq.answer = AsyncMock()
    cq.edit_message_text = AsyncMock(side_effect=render_side_effect)
    update = MagicMock()
    update.callback_query = cq
    update.message = None
    update.effective_user = MagicMock(id=321)
    return update, cq


def _ctx():
    ctx = MagicMock()
    ctx.user_data = {"language": "en", "authenticated": True, "staff_roles": ["delivery_driver"]}
    ctx.bot = MagicMock()
    return ctx


@pytest.mark.unit
class TestReconciliationStartRollback:
    def test_render_failure_rolls_back_reconciliation_flag(self, monkeypatch):
        from staff_bot.utils import flow_state

        monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
        clear_active = AsyncMock()
        monkeypatch.setattr(flow_state, "clear_active", clear_active)

        handler = _handler()
        update, _ = _callback_update(render_side_effect=RuntimeError("render boom"))
        ctx = _ctx()

        with pytest.raises(RuntimeError):
            asyncio.run(handler.start_reconciliation_submit(update, ctx))

        # The flag must NOT survive a failed prompt render — otherwise the next
        # text the driver sends is parsed as reconciliation cash with no UI.
        assert "pending_reconciliation_flow" not in ctx.user_data
        clear_active.assert_awaited_once()

    def test_successful_render_arms_reconciliation_flag(self, monkeypatch):
        from staff_bot.utils import flow_state

        monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
        monkeypatch.setattr(flow_state, "clear_active", AsyncMock())

        handler = _handler()
        update, cq = _callback_update()
        ctx = _ctx()

        asyncio.run(handler.start_reconciliation_submit(update, ctx))

        assert ctx.user_data.get("pending_reconciliation_flow") == {"action": "submit"}
        cq.edit_message_text.assert_awaited_once()
