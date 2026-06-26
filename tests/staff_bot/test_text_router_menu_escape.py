"""Reply-keyboard MENU taps must always navigate, even mid-flow.

Regression suite for the staff-bot text-router state-leak bug class: the
permanently-visible reply keyboard (MenuKeyboards.main_menu) sends TEXT, and
``StaffBot._handle_text_message`` used to route that text into whatever
``pending_*_flow`` was armed before it ever checked whether the text was a menu
label. Symptoms ranged from "Invalid cash amount" (the reported bug) to a menu
tap being consumed as a NOTE that finalized a real transaction.

These tests drive ``_handle_text_message`` directly with spy handlers and assert
the routing *decision*: a menu-label tap clears every flow and dispatches to the
menu; non-menu text (amounts/notes) still reaches the flow handler.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.bot import StaffBot
from staff_bot.i18n import i18n


def _make_bot():
    """A StaffBot wired with spy handlers (no __init__, no network)."""
    bot = StaffBot.__new__(StaffBot)

    lang = MagicMock()
    lang._get_language = AsyncMock(return_value="en")
    lang.language_menu = AsyncMock()
    bot._language_handler = lang

    status_update = MagicMock()
    status_update.show_cash_hub = AsyncMock()
    status_update.receive_cash_amount = AsyncMock()
    status_update.receive_cash_note = AsyncMock()
    status_update.receive_bottle_count = AsyncMock()
    status_update.receive_reconciliation_declared_cash = AsyncMock()

    cash_collection = MagicMock()
    cash_collection.receive_collection_amount = AsyncMock()
    cash_collection.receive_collection_note = AsyncMock()

    active_delivery = MagicMock()
    active_delivery.show_active_deliveries = AsyncMock()

    bottle_collection = MagicMock()
    bottle_collection.receive_collection_note = AsyncMock()
    bottle_collection.receive_fine_bottle_qty = AsyncMock()
    bottle_collection.receive_fine_amount = AsyncMock()
    bottle_collection.receive_fine_note = AsyncMock()

    tryouts = MagicMock()
    tryouts.show_hub = AsyncMock()
    tryouts.receive_pickup_quantities = AsyncMock()

    bot._delivery_handlers = {
        "status_update": status_update,
        "cash_collection": cash_collection,
        "active_delivery": active_delivery,
        "bottle_collection": bottle_collection,
        "tryouts": tryouts,
    }

    profile = MagicMock()
    profile.show_profile = AsyncMock()
    help_h = MagicMock()
    help_h.show_help = AsyncMock()
    bot._common_handlers = {"profile": profile, "help": help_h}

    bot._route_new_orders = AsyncMock()
    return bot


def _update(text):
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 555
    update.callback_query = None
    return update


def _ctx(user_data):
    ctx = MagicMock()
    ctx.user_data = user_data
    ctx.bot = MagicMock()
    ctx.bot_data = {}
    return ctx


def _label(menu_key):
    """The exact text the reply keyboard emits, e.g. '💰 Cash'."""
    emoji = {
        "cash": "💰",
        "new_orders": "📦",
        "active_deliveries": "🚚",
        "tryouts": "🧪",
        "profile": "👤",
        "settings": "⚙️",
        "help": "❓",
    }[menu_key]
    return f"{emoji} {i18n.get(f'staff.menu.{menu_key}', 'en')}"


@pytest.mark.unit
class TestMenuTapEscapesArmedFlow:
    def test_reported_bug_menu_tap_during_cod_statement_view(self):
        """The exact reported bug: COD flow armed (amount None), tap 'Cash'."""
        bot = _make_bot()
        ud = {
            "authenticated": True,
            "pending_cod_collection_flow": {"customer_id": 1, "total_outstanding_amount": 1000},
        }
        update = _update(_label("cash"))
        ctx = _ctx(ud)

        asyncio.run(bot._handle_text_message(update, ctx))

        bot._delivery_handlers["cash_collection"].receive_collection_amount.assert_not_called()
        bot._delivery_handlers["status_update"].show_cash_hub.assert_awaited_once()
        assert "pending_cod_collection_flow" not in ud

    def test_menu_tap_during_cod_note_step_does_not_record_payment(self):
        """COD flow armed at the NOTE step (amount set): a menu tap must not be
        consumed as the note (which would record a COD payment)."""
        bot = _make_bot()
        ud = {
            "authenticated": True,
            "pending_cod_collection_flow": {"customer_id": 1, "amount": 5000},
        }
        update = _update(_label("new_orders"))
        ctx = _ctx(ud)

        asyncio.run(bot._handle_text_message(update, ctx))

        bot._delivery_handlers["cash_collection"].receive_collection_note.assert_not_called()
        bot._route_new_orders.assert_awaited_once()
        assert "pending_cod_collection_flow" not in ud

    def test_menu_tap_during_cash_note_step_does_not_finalize_delivery(self):
        """The dangerous one: no-cash delivery note step accepts any non-empty
        text. A menu tap must navigate, not finalize the delivery."""
        bot = _make_bot()
        ud = {
            "authenticated": True,
            "pending_delivery_cash_flow": {"delivery_id": 9, "flow_type": "none", "cash_amount": 0.0},
        }
        update = _update(_label("active_deliveries"))
        ctx = _ctx(ud)

        asyncio.run(bot._handle_text_message(update, ctx))

        bot._delivery_handlers["status_update"].receive_cash_note.assert_not_called()
        bot._delivery_handlers["active_delivery"].show_active_deliveries.assert_awaited_once()
        assert "pending_delivery_cash_flow" not in ud

    def test_menu_tap_during_partial_cash_amount_step(self):
        bot = _make_bot()
        ud = {
            "authenticated": True,
            "pending_delivery_cash_flow": {"delivery_id": 9, "flow_type": "partial"},
        }
        update = _update(_label("cash"))
        ctx = _ctx(ud)

        asyncio.run(bot._handle_text_message(update, ctx))

        bot._delivery_handlers["status_update"].receive_cash_amount.assert_not_called()
        bot._delivery_handlers["status_update"].show_cash_hub.assert_awaited_once()
        assert "pending_delivery_cash_flow" not in ud

    def test_menu_tap_during_reconciliation_clears_flow(self):
        bot = _make_bot()
        ud = {"authenticated": True, "pending_reconciliation_flow": {"action": "submit"}}
        update = _update(_label("profile"))
        ctx = _ctx(ud)

        asyncio.run(bot._handle_text_message(update, ctx))

        bot._delivery_handlers["status_update"].receive_reconciliation_declared_cash.assert_not_called()
        bot._common_handlers["profile"].show_profile.assert_awaited_once()
        assert "pending_reconciliation_flow" not in ud

    def test_menu_tap_during_bottle_fine_note_does_not_create_fine(self):
        bot = _make_bot()
        ud = {
            "authenticated": True,
            "pending_bottle_collection_flow": {
                "customer_id": 1, "address_id": 2, "action": "fine",
                "fine_quantity": 3, "fine_amount": 10000,
            },
        }
        update = _update(_label("cash"))
        ctx = _ctx(ud)

        asyncio.run(bot._handle_text_message(update, ctx))

        bot._delivery_handlers["bottle_collection"].receive_fine_note.assert_not_called()
        bot._delivery_handlers["status_update"].show_cash_hub.assert_awaited_once()
        assert "pending_bottle_collection_flow" not in ud

    def test_menu_tap_during_tryout_pickup_clears_all_pickup_keys(self):
        bot = _make_bot()
        ud = {
            "authenticated": True,
            "tryout_pickup_task_id": 7,
            "tryout_pickup_products": [{"id": 1}],
            "tryout_pickup_state": {"task_id": 7},
        }
        update = _update(_label("cash"))
        ctx = _ctx(ud)

        asyncio.run(bot._handle_text_message(update, ctx))

        bot._delivery_handlers["tryouts"].receive_pickup_quantities.assert_not_called()
        bot._delivery_handlers["status_update"].show_cash_hub.assert_awaited_once()
        assert "tryout_pickup_task_id" not in ud
        assert "tryout_pickup_products" not in ud
        assert "tryout_pickup_state" not in ud


@pytest.mark.unit
class TestLegitimateFlowInputStillRoutes:
    """The fix must NOT break real typed input — amounts/notes still flow."""

    def test_numeric_amount_during_cod_flow_routes_to_amount_handler(self):
        bot = _make_bot()
        ud = {"authenticated": True, "pending_cod_collection_flow": {"customer_id": 1}}
        update = _update("54000")
        ctx = _ctx(ud)

        asyncio.run(bot._handle_text_message(update, ctx))

        bot._delivery_handlers["cash_collection"].receive_collection_amount.assert_awaited_once()
        bot._delivery_handlers["status_update"].show_cash_hub.assert_not_called()
        assert "pending_cod_collection_flow" in ud

    def test_free_text_note_during_cod_note_step_routes_to_note_handler(self):
        bot = _make_bot()
        ud = {"authenticated": True, "pending_cod_collection_flow": {"customer_id": 1, "amount": 5000}}
        update = _update("paid at the door")
        ctx = _ctx(ud)

        asyncio.run(bot._handle_text_message(update, ctx))

        bot._delivery_handlers["cash_collection"].receive_collection_note.assert_awaited_once()
        assert "pending_cod_collection_flow" in ud

    def test_unauthenticated_text_is_ignored(self):
        bot = _make_bot()
        ud = {"authenticated": False, "pending_cod_collection_flow": {"customer_id": 1}}
        update = _update(_label("cash"))
        ctx = _ctx(ud)

        asyncio.run(bot._handle_text_message(update, ctx))

        bot._delivery_handlers["status_update"].show_cash_hub.assert_not_called()
        bot._delivery_handlers["cash_collection"].receive_collection_amount.assert_not_called()
