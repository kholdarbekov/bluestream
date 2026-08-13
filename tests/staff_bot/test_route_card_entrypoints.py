"""show_active_deliveries now renders THE route card: one render call with
the tap's chat/message as reference (repost heuristic input), API errors
still routed through the shared error helpers, and the view-switch callbacks
registered in bot.py.

Fix round 1 additions (review findings I1-I3):
- TestDoubleAnswerGuard: `optimize_routes` answers its callback_query, then
  calls `show_active_deliveries` -- which used to bare-`.answer()` the SAME
  query again. Proves the second answer is guarded (doesn't raise, doesn't
  block the render).
- TestSwitchRouteView: `switch_route_view` invoked for real (not mocked
  out), through a real `render_route_card` + fake Redis, proving it derives
  the view from `query.data` (not a hand-set kwarg) and edits the existing
  card rather than sending a new one.
- TestWiring.test_switch_route_view_registered_as_real_callback_handler:
  AST-based, not substring-in-text -- a commented-out registration line
  contains the same substrings and would have passed the old test.
"""

import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from staff_bot.handlers.delivery import active_delivery as mod
from staff_bot.handlers.delivery import route_card
from staff_bot.handlers.delivery.active_delivery import ActiveDeliveryHandler
from staff_bot.utils import route_card_state

BOT_PY = Path(__file__).resolve().parents[2] / "staff_bot" / "bot.py"


class _Api:
    def __init__(self, response):
        self.client = MagicMock()
        self.client.get_active_deliveries = AsyncMock(return_value=response)

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *a):
        return False


def _payload():
    return {"items": [], "total": 0, "location_status": "missing", "route_summary": {}}


def _callback_update(chat_id=777, msg_id=555, user_id=777, data="staff_active_deliveries"):
    update = MagicMock()
    update.effective_user.id = user_id
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message = MagicMock()
    update.callback_query.message.chat.id = chat_id
    update.callback_query.message.message_id = msg_id
    return update


def _ctx():
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.user_data = {}
    return ctx


def _driver_ctx(bot=None, **extra_user_data):
    """A context that satisfies `require_auth`/`require_delivery_driver`
    for real, so the decorated handler methods can be invoked directly
    instead of through their undecorated inner methods."""
    ctx = MagicMock()
    ctx.bot = bot or MagicMock()
    ctx.user_data = {
        "authenticated": True,
        "staff_roles": ["delivery_driver"],
        "language": "en",
        **extra_user_data,
    }
    return ctx


@pytest.mark.unit
class TestRenderFromUpdate:
    def test_callback_passes_chat_and_reference_to_render(self, monkeypatch):
        render = AsyncMock()
        monkeypatch.setattr(route_card, "render_route_card", render)
        monkeypatch.setattr(
            mod, "api_client", _Api(MagicMock(success=True, data=_payload()))
        )
        handler = ActiveDeliveryHandler()
        update = _callback_update(chat_id=42, msg_id=900)
        ctx = _ctx()

        asyncio.run(handler._render_card_from_update(update, ctx, "en", "tok"))

        render.assert_awaited_once()
        kwargs = render.await_args.kwargs
        assert kwargs["telegram_id"] == 777
        assert kwargs["chat_id"] == 42
        assert kwargs["reference_message_id"] == 900
        assert kwargs["view"] is None
        assert kwargs["payload"]["location_status"] == "missing"

    def test_menu_text_entry_uses_message_as_reference(self, monkeypatch):
        render = AsyncMock()
        monkeypatch.setattr(route_card, "render_route_card", render)
        monkeypatch.setattr(
            mod, "api_client", _Api(MagicMock(success=True, data=_payload()))
        )
        handler = ActiveDeliveryHandler()
        update = MagicMock()
        update.effective_user.id = 777
        update.callback_query = None
        update.message = MagicMock()
        update.message.chat.id = 777
        update.message.message_id = 1234

        asyncio.run(handler._render_card_from_update(update, _ctx(), "en", "tok"))

        assert render.await_args.kwargs["reference_message_id"] == 1234

    def test_api_401_routes_to_auth_error_no_render(self, monkeypatch):
        render = AsyncMock()
        monkeypatch.setattr(route_card, "render_route_card", render)
        monkeypatch.setattr(
            mod, "api_client",
            _Api(MagicMock(success=False, status_code=401, error="expired")),
        )
        handler = ActiveDeliveryHandler()
        auth_err = AsyncMock()
        monkeypatch.setattr(handler, "_handle_auth_error", auth_err)

        asyncio.run(handler._render_card_from_update(_callback_update(), _ctx(), "en", "tok"))

        auth_err.assert_awaited_once()
        render.assert_not_called()

    def test_view_param_forwarded(self, monkeypatch):
        """`_render_card_from_update`'s own `view` kwarg (as opposed to a
        value derived from callback data -- see TestSwitchRouteView for
        that) is forwarded to the renderer unchanged."""
        render = AsyncMock()
        monkeypatch.setattr(route_card, "render_route_card", render)
        monkeypatch.setattr(
            mod, "api_client", _Api(MagicMock(success=True, data=_payload()))
        )
        handler = ActiveDeliveryHandler()

        asyncio.run(handler._render_card_from_update(
            _callback_update(), _ctx(), "en", "tok", view="all"
        ))

        assert render.await_args.kwargs["view"] == "all"


def _route_items():
    return [{
        "delivery_id": 11, "order_number": "1042", "status": "assigned",
        "customer_name": "U", "customer_phone": "+998900000001",
        "district": "Chilanzar", "address": "Street 1", "items": [],
        "total_amount": 10000, "payment_method": "cash",
        "amount_collected": 0, "outstanding_amount": 10000,
        "expected_cash_to_collect": 10000, "cod_reserved_prepayment_amount": 0,
        "destination_latitude": 41.31, "destination_longitude": 69.27,
        "route_position": 0, "is_next": True,
        "eta_minutes_from_current_location": None, "distance_km_to_next": None,
    }]


def _route_payload():
    items = _route_items()
    return {
        "items": items, "total": len(items), "location_status": "fresh",
        "route_summary": {
            "remaining": len(items), "stops_completed_today": 0,
            "stops_total_today": len(items), "committed_delivery_id": None,
            "finish_eta": None, "updated_at": None,
        },
    }


class _FakeRedis:
    """Just enough of redis.asyncio for set/get/delete with ex= (same shape
    as test_route_card_render.py / test_route_card_state.py)."""

    def __init__(self):
        self.store = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


def _fake_bot(message_id=100):
    bot = MagicMock()
    sent = MagicMock()
    sent.chat_id = 777
    sent.message_id = message_id
    bot.send_message = AsyncMock(return_value=sent)
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    bot.pin_chat_message = AsyncMock()
    return bot


@pytest.mark.unit
class TestSwitchRouteView:
    """The one genuinely new production method (review round 1, I3) --
    invoked for real, through a real `render_route_card` and a fake Redis,
    not a mocked-out render function. Proves the view really comes from
    `query.data`, and that flipping views edits the existing pinned card
    rather than sending a new one."""

    @pytest.fixture(autouse=True)
    def _reset_route_card_state(self):
        route_card_state.configure(_FakeRedis())
        route_card_state._locks.clear()
        yield
        route_card_state.configure(None)
        route_card_state._locks.clear()

    def test_derives_view_from_callback_data_and_edits_existing_card(self, monkeypatch):
        bot = _fake_bot()
        # Seed an existing "next" view card, as if the driver already had
        # the route card open -- this is the state switch_route_view must
        # edit in place rather than replace.
        asyncio.run(route_card.render_route_card(
            bot, telegram_id=777, chat_id=777, language="en",
            payload=_route_payload(),
        ))
        bot.send_message.assert_awaited_once()  # sanity: the seed created it
        next_view_text = bot.send_message.await_args.kwargs["text"]
        assert "SUGGESTED NEXT" in next_view_text.upper() or "1042" in next_view_text

        monkeypatch.setattr(
            mod, "api_client", _Api(MagicMock(success=True, data=_route_payload()))
        )
        handler = ActiveDeliveryHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
        # The tap: "All stops" button on the card just seeded above, so the
        # reference message id matches the card -- no repost.
        update = _callback_update(chat_id=777, msg_id=100, user_id=777, data="staff_route_view_all")
        ctx = _driver_ctx(bot=bot)

        asyncio.run(handler.switch_route_view(update, ctx))

        # Flipping views must EDIT the seeded card, never send a second one.
        update.callback_query.answer.assert_awaited_once()
        bot.send_message.assert_awaited_once()  # still just the one from the seed
        bot.edit_message_text.assert_awaited_once()
        edited_text = bot.edit_message_text.await_args.kwargs["text"]
        assert edited_text != next_view_text
        assert "1. #1042" in edited_text  # all-view's numbered listing

        state = asyncio.run(route_card_state.load(777))
        assert state["view"] == route_card_state.VIEW_ALL

    def test_staff_route_view_next_data_switches_back(self, monkeypatch):
        """The companion direction, so the test genuinely depends on the
        `data=` value rather than always landing on "all" by luck."""
        bot = _fake_bot()
        asyncio.run(route_card_state.save(777, {
            "chat_id": 777, "message_id": 100,
            "card_date": route_card.local_date_str(),
            "view": route_card_state.VIEW_ALL, "content_sig": "stale",
        }))
        monkeypatch.setattr(
            mod, "api_client", _Api(MagicMock(success=True, data=_route_payload()))
        )
        handler = ActiveDeliveryHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
        update = _callback_update(chat_id=777, msg_id=100, user_id=777, data="staff_route_view_next")
        ctx = _driver_ctx(bot=bot)

        asyncio.run(handler.switch_route_view(update, ctx))

        bot.edit_message_text.assert_awaited_once()
        edited_text = bot.edit_message_text.await_args.kwargs["text"]
        assert "1. #1042" not in edited_text  # next-view, not the numbered list
        state = asyncio.run(route_card_state.load(777))
        assert state["view"] == route_card_state.VIEW_NEXT

    def test_unauthenticated_driver_is_blocked(self, monkeypatch):
        """The auth/role guards (`@require_auth`/`@require_delivery_driver`)
        must still gate this new method like every other handler."""
        render = AsyncMock()
        monkeypatch.setattr(route_card, "render_route_card", render)
        handler = ActiveDeliveryHandler()
        update = _callback_update(data="staff_route_view_all")
        ctx = MagicMock()
        ctx.user_data = {}  # not authenticated

        asyncio.run(handler.switch_route_view(update, ctx))

        render.assert_not_called()


@pytest.mark.unit
class TestDoubleAnswerGuard:
    """`optimize_routes` answers its callback_query, then (on two paths)
    calls `show_active_deliveries`, which itself answers the SAME query
    again. Review round 1, I1: that second answer must be guarded -- a
    raised BadRequest must not block the render, and the failure must not
    propagate into `_handle_error` (which would show the driver an error
    alert after already showing them the real one)."""

    def test_double_answer_failure_does_not_block_render_or_raise(self, monkeypatch):
        render = AsyncMock()
        monkeypatch.setattr(route_card, "render_route_card", render)
        monkeypatch.setattr(
            mod, "api_client", _Api(MagicMock(success=True, data=_payload()))
        )
        handler = ActiveDeliveryHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
        update = _callback_update()
        # Simulates Telegram rejecting a second answerCallbackQuery for a
        # query already answered once (by optimize_routes, in the real flow).
        update.callback_query.answer = AsyncMock(
            side_effect=BadRequest("Query is too old and response timeout expired")
        )
        handle_error = AsyncMock()
        monkeypatch.setattr(handler, "_handle_error", handle_error)
        ctx = _driver_ctx()

        asyncio.run(handler.show_active_deliveries(update, ctx))

        render.assert_awaited_once()  # the render still happened
        handle_error.assert_not_called()  # the answer failure was swallowed

    def test_optimize_routes_route_locked_alert_survives_the_second_answer(self, monkeypatch):
        """End-to-end reproduction of the exact call site (:271):
        optimize_routes shows the "locked by dispatch" alert, THEN calls
        show_active_deliveries, whose own answer() must not blow that up."""
        handler = ActiveDeliveryHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
        render = AsyncMock()
        monkeypatch.setattr(route_card, "render_route_card", render)
        # optimize_routes calls client.optimize_route(...) first; the
        # subsequent internal show_active_deliveries call then re-enters
        # the same api_client and calls client.get_active_deliveries(...)
        # -- both need real awaitables on the SAME client object.
        api = _Api(MagicMock(success=True, data=_payload()))
        api.client.optimize_route = AsyncMock(
            return_value=MagicMock(success=True, data={"route_locked": True})
        )
        monkeypatch.setattr(mod, "api_client", api)
        update = _callback_update(data="staff_optimize_routes")
        # First call: optimize_routes's own "locked by dispatch" alert,
        # succeeds. Second and third calls: show_active_deliveries's
        # internal answer of the SAME query, rejected -- BadRequest is a
        # NetworkError subclass in python-telegram-bot, so
        # `_safe_callback_answer` retries it once (base.py
        # TELEGRAM_RETRY_ATTEMPTS=2) before giving up; both attempts fail
        # exactly like a real double-answer would.
        update.callback_query.answer = AsyncMock(side_effect=[
            None,
            BadRequest("Query is too old"),
            BadRequest("Query is too old"),
        ])
        ctx = _driver_ctx()

        asyncio.run(handler.optimize_routes(update, ctx))

        assert update.callback_query.answer.await_count == 3
        first_call_kwargs = update.callback_query.answer.await_args_list[0].kwargs
        assert first_call_kwargs.get("show_alert") is True  # the real alert, unharmed
        render.assert_awaited_once()  # show_active_deliveries's render still ran


@pytest.mark.unit
class TestWiring:
    def test_switch_route_view_registered_as_real_callback_handler(self):
        """Substring-in-file-text would still pass with the registration
        line commented out (review round 1, I3). Parse the actual AST
        instead: a real `CallbackQueryHandler(...)` call, not a comment or
        a string, whose first arg is `....switch_route_view` and whose
        `pattern=` is exactly the view-switch regex, must exist."""
        tree = ast.parse(BOT_PY.read_text(encoding="utf-8"), filename=str(BOT_PY))
        found = False
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "CallbackQueryHandler"):
                continue
            if not node.args:
                continue
            callback_arg = node.args[0]
            if not (isinstance(callback_arg, ast.Attribute) and callback_arg.attr == "switch_route_view"):
                continue
            pattern_kw = next((kw for kw in node.keywords if kw.arg == "pattern"), None)
            if (
                pattern_kw is not None
                and isinstance(pattern_kw.value, ast.Constant)
                and pattern_kw.value.value == "^staff_route_view_(next|all)$"
            ):
                found = True
                break
        assert found, (
            "No live CallbackQueryHandler(<handler>.switch_route_view, "
            "pattern='^staff_route_view_(next|all)$') call found in bot.py"
        )

    def test_old_machinery_is_gone(self):
        """The delete-and-resend lifecycle must not survive as dead code —
        two render paths for one surface is the bug class this plan removes."""
        for name in (
            "_render_active_deliveries", "_delete_previous_card_messages",
            "_render_header", "_CARDS_KEY", "_HEADER_KEY", "_HEADER_SIG_KEY",
            "_RENDER_LOCK_KEY",
        ):
            assert not hasattr(mod, name) and not hasattr(ActiveDeliveryHandler, name), name
