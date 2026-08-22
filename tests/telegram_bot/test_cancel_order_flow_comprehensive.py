"""Comprehensive regression coverage for the cancel-order ``No`` flow.

Prod incident
-------------
Tapping "No" on the order-cancel confirmation crashed with::

    AttributeError: Attribute `data` of class `CallbackQuery` can't be set!

because ``cancel_order_confirm_no`` re-routed into ``order_details`` by mutating
the callback data (``query.data = f"order_{id}"``). python-telegram-bot's
``CallbackQuery`` is a *frozen* object, so the assignment raised and the user
never returned to the order detail view.

The fix gives ``order_details(update, context, order_id=None)`` a second entry
point: when ``order_id`` is supplied explicitly it is used verbatim and the
callback data is NOT parsed; ``cancel_order_confirm_no`` now calls
``self.order_details(update, context, order_id=order_id)`` instead of poking the
immutable attribute.

The old suite missed this because it mocked ``order_details`` away (never
exercising the immutable-attribute path) and/or used a mutable
``DummyCallbackQuery`` whose ``data`` setter happily accepts assignment — hiding
the very crash that happened in prod. These tests:

* drive ``cancel_order_confirm_no`` with a FROZEN callback query that raises on
  ``.data`` assignment (mirroring real PTB), and assert it returns to details
  WITHOUT mutating ``data`` and WITHOUT hitting ``_handle_error``;
* pin the ``order_details`` dual-entry contract (explicit ``order_id`` wins and
  callback data is never parsed; absent ``order_id`` parses ``order_<id>``);
* run a full "open order -> Cancel -> No" sequence end-to-end with a real
  (frozen) callback query and a fake API client, proving no ``AttributeError``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import orders as orders_module
from tests.telegram_bot.helpers import (
    DummyCallbackQuery,
    DummyMessage,
    DummyUpdate,
    make_context,
)
from tests.telegram_bot.ptb_harness import (
    DEFAULT_CHAT_ID,
    DEFAULT_USER_ID,
    build_bot_harness,
)


def _resp(success=True, data=None, error=None, status_code=200):
    return SimpleNamespace(success=success, data=data or {}, error=error, status_code=status_code)


def _i18n_get(key, language, *args, **kwargs):
    return f"{key}:{language}"


class FrozenCallbackQuery:
    """Mimics python-telegram-bot's immutable CallbackQuery: ``data`` can't be set.

    Reuses the exact pattern from
    ``tests/telegram_bot/test_handlers_cancel_order_confirm_no.py`` so the
    regression is reproduced faithfully: any attempt to assign ``.data`` raises
    ``AttributeError`` just like the frozen PTB object did in prod.
    """

    def __init__(self, data: str = "noop"):
        object.__setattr__(self, "_data", data)
        self.message = DummyMessage()
        self.answer = AsyncMock()
        self.edit_message_text = AsyncMock()

    @property
    def data(self):
        return self._data

    @data.setter
    def data(self, value):  # noqa: D401 - mirrors PTB's frozen-object error
        raise AttributeError("Attribute `data` of class `CallbackQuery` can't be set!")


def _minimal_order(order_id: int):
    """A minimal but complete-enough order payload for order_details to render."""
    return {
        "id": order_id,
        "order_number": f"TG_{order_id}",
        "created_at": "2026-06-21T10:00:00",
        "total_amount": 18000,
        "status": "confirmed",
        "order_items": [],
    }


def _get_order_ok(order_id: int):
    return _resp(success=True, data={"data": {"order": _minimal_order(order_id), "delivery": None}})


class RecordingAPIContext:
    """``async with api_client`` stub that records the order id passed to get_order.

    ``FakeAPIClientContext`` stores method *results*, so it can't tell us which
    order id the handler actually requested. This records every ``get_order``
    call's id and returns a successful payload for that exact id — letting us
    assert the dual-entry routing sent the right id to the API.
    """

    def __init__(self):
        self.requested_ids = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def get_order(self, _token, order_id, *_args, **_kwargs):
        self.requested_ids.append(order_id)
        return _get_order_ok(order_id)


@pytest.mark.unit
@pytest.mark.anyio
class TestCancelOrderConfirmNoFrozenQuery:
    """``cancel_order_confirm_no`` must never mutate the frozen CallbackQuery."""

    async def test_returns_to_order_details_without_mutating_frozen_data(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        handler.order_details = AsyncMock()
        handler._handle_error = AsyncMock()

        update = DummyUpdate()
        # The id rides on the callback data — the only carrier that survives a
        # redeploy — and is READ from it, never written back onto it.
        update.callback_query = FrozenCallbackQuery(data="cancel_order_42_confirm_no")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))

        await handler.cancel_order_confirm_no(update, context)

        # Re-dispatches with the id passed EXPLICITLY (the fix), not via data.
        handler.order_details.assert_awaited_once_with(update, context, order_id=42)
        # Frozen callback data is untouched.
        assert update.callback_query.data == "cancel_order_42_confirm_no"
        # The immutable-attribute crash never reached the error handler.
        handler._handle_error.assert_not_awaited()
        # Nothing about this flow is kept in bot memory any more.
        assert "cancelling_order_id" not in context.user_data

    async def test_does_not_attempt_data_assignment_even_with_real_order_details(self, monkeypatch):
        """End-to-end with a frozen query AND a real (non-mocked) order_details.

        This is the path that crashed in prod: order_details runs for real, so if
        the handler ever reached for ``query.data = ...`` the frozen setter would
        raise. Here it must complete cleanly and render the detail view.
        """
        handler = orders_module.OrderHandlers()
        handler._handle_error = AsyncMock()

        update = DummyUpdate()
        update.callback_query = FrozenCallbackQuery(data="cancel_order_314_confirm_no")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "order_details", lambda *_a, **_k: "kbd")
        api = RecordingAPIContext()
        monkeypatch.setattr(orders_module, "api_client", api)

        await handler.cancel_order_confirm_no(update, context)

        # The exact order id was requested from the API (passed through, not parsed).
        assert api.requested_ids == [314]
        # The detail view was rendered on the (frozen) query.
        update.callback_query.edit_message_text.assert_awaited_once()
        # Frozen data still intact; no crash bubbled into the error handler.
        assert update.callback_query.data == "cancel_order_314_confirm_no"
        handler._handle_error.assert_not_awaited()
        assert "cancelling_order_id" not in context.user_data

    async def test_a_callback_with_no_order_id_tells_the_customer(self, monkeypatch):
        """A card from before this release carries no id: SAY so, do no work.

        The old handler answered such a tap with an empty ``query.answer()``
        and returned, so the Yes/No card stayed on screen looking live and the
        customer had no way to tell it was dead. Now the tap is answered with
        text and the orders list is re-rendered underneath it.
        """
        handler = orders_module.OrderHandlers()
        handler.order_details = AsyncMock()
        handler.orders_menu = AsyncMock()
        handler._handle_error = AsyncMock()

        update = DummyUpdate()
        update.callback_query = FrozenCallbackQuery(data="cancel_order_confirm_no")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)

        await handler.cancel_order_confirm_no(update, context)

        update.callback_query.answer.assert_awaited_once_with("telegram.error.generic:en")
        handler.orders_menu.assert_awaited_once_with(update, context)
        handler.order_details.assert_not_awaited()
        handler._handle_error.assert_not_awaited()

    async def test_zero_order_id_on_the_callback_is_treated_as_absent(self, monkeypatch):
        """A falsy (0) order id must not re-dispatch into order_details."""
        handler = orders_module.OrderHandlers()
        handler.order_details = AsyncMock()
        handler.orders_menu = AsyncMock()
        handler._handle_error = AsyncMock()

        update = DummyUpdate()
        update.callback_query = FrozenCallbackQuery(data="cancel_order_0_confirm_no")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)

        await handler.cancel_order_confirm_no(update, context)

        update.callback_query.answer.assert_awaited_once_with("telegram.error.generic:en")
        handler.orders_menu.assert_awaited_once_with(update, context)
        handler.order_details.assert_not_awaited()
        handler._handle_error.assert_not_awaited()

    async def test_order_details_failure_is_caught_by_handle_error(self, monkeypatch):
        """If order_details raises, the try/except still guards the handler.

        The operation tag must be ``cancel_order_confirm_no`` (not
        ``order_details``) so the failing handler is correctly attributed.
        """
        handler = orders_module.OrderHandlers()
        boom = RuntimeError("render exploded")
        handler.order_details = AsyncMock(side_effect=boom)
        handler._handle_error = AsyncMock()

        update = DummyUpdate()
        update.callback_query = FrozenCallbackQuery(data="cancel_order_77_confirm_no")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))

        await handler.cancel_order_confirm_no(update, context)

        handler.order_details.assert_awaited_once_with(update, context, order_id=77)
        handler._handle_error.assert_awaited_once()
        kwargs = handler._handle_error.await_args.kwargs
        assert kwargs.get("exc") is boom
        assert kwargs.get("operation") == "cancel_order_confirm_no"

    async def test_handle_error_invoked_if_get_user_language_fails(self, monkeypatch):
        """Defensive: an early failure (before the order_id check) is also guarded."""
        handler = orders_module.OrderHandlers()
        handler.order_details = AsyncMock()
        handler._handle_error = AsyncMock()

        update = DummyUpdate()
        update.callback_query = FrozenCallbackQuery(data="cancel_order_5_confirm_no")
        context = make_context()

        monkeypatch.setattr(
            orders_module.i18n, "get_user_language", AsyncMock(side_effect=RuntimeError("i18n down"))
        )

        await handler.cancel_order_confirm_no(update, context)

        handler.order_details.assert_not_awaited()
        handler._handle_error.assert_awaited_once()
        assert handler._handle_error.await_args.kwargs.get("operation") == "cancel_order_confirm_no"


@pytest.mark.unit
@pytest.mark.anyio
class TestOrderDetailsDualEntryContract:
    """``order_details`` parses callback data ONLY when ``order_id`` is None."""

    async def test_explicit_order_id_ignores_non_order_callback_data(self, monkeypatch):
        """Explicit order_id is used verbatim even when data is NOT an order_<id> string.

        This is the crux of the fix: ``cancel_order_confirm_no`` passes the id
        explicitly while the callback data is still ``cancel_order_confirm_no``.
        If the handler parsed data it would blow up on ``int('order'...)``.
        """
        handler = orders_module.OrderHandlers()
        handler._handle_error = AsyncMock()

        update = DummyUpdate()
        # data is decidedly NOT 'order_<id>' — parsing it would raise.
        update.callback_query = DummyCallbackQuery(data="cancel_order_confirm_no")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "order_details", lambda *_a, **_k: "kbd")
        api = RecordingAPIContext()
        monkeypatch.setattr(orders_module, "api_client", api)

        await handler.order_details(update, context, order_id=555)

        # The explicit id reached the API; callback data was never parsed.
        assert api.requested_ids == [555]
        update.callback_query.edit_message_text.assert_awaited_once()
        handler._handle_error.assert_not_awaited()

    async def test_explicit_order_id_with_garbage_data_does_not_raise(self, monkeypatch):
        """Even outright malformed data (no underscore) is irrelevant when id is explicit."""
        handler = orders_module.OrderHandlers()
        handler._handle_error = AsyncMock()

        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="totally-not-parseable")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "order_details", lambda *_a, **_k: "kbd")
        api = RecordingAPIContext()
        monkeypatch.setattr(orders_module, "api_client", api)

        await handler.order_details(update, context, order_id=99)

        assert api.requested_ids == [99]
        handler._handle_error.assert_not_awaited()

    async def test_without_order_id_parses_id_from_callback_data(self, monkeypatch):
        """The registered '^order_' handler path: parse 'order_789' -> 789."""
        handler = orders_module.OrderHandlers()
        handler._handle_error = AsyncMock()

        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="order_789")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "order_details", lambda *_a, **_k: "kbd")
        api = RecordingAPIContext()
        monkeypatch.setattr(orders_module, "api_client", api)

        await handler.order_details(update, context)

        assert api.requested_ids == [789]
        update.callback_query.edit_message_text.assert_awaited_once()
        handler._handle_error.assert_not_awaited()

    async def test_without_order_id_malformed_callback_data_hits_handle_error(self, monkeypatch):
        """No explicit id + unparseable data -> the parse raises and is guarded."""
        handler = orders_module.OrderHandlers()
        handler._handle_error = AsyncMock()

        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="order_notanumber")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)

        await handler.order_details(update, context)

        handler._handle_error.assert_awaited_once()
        kwargs = handler._handle_error.await_args.kwargs
        assert isinstance(kwargs.get("exc"), (ValueError, IndexError))
        assert kwargs.get("operation") == "order_details"

    async def test_explicit_order_id_does_not_consult_callback_data_attribute(self, monkeypatch):
        """Belt-and-braces: with explicit id, query.data is never even read.

        We hand it a callback query whose ``data`` raises if accessed, proving
        the dual-entry guard short-circuits before touching the attribute.
        """
        handler = orders_module.OrderHandlers()
        handler._handle_error = AsyncMock()

        class _DataExplodes:
            def __init__(self):
                self.message = DummyMessage()
                self.answer = AsyncMock()
                self.edit_message_text = AsyncMock()

            @property
            def data(self):
                raise AssertionError("order_details must not read query.data when order_id is explicit")

        update = DummyUpdate()
        update.callback_query = _DataExplodes()
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "order_details", lambda *_a, **_k: "kbd")
        api = RecordingAPIContext()
        monkeypatch.setattr(orders_module, "api_client", api)

        await handler.order_details(update, context, order_id=12)

        assert api.requested_ids == [12]
        update.callback_query.edit_message_text.assert_awaited_once()
        handler._handle_error.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.anyio
class TestCancelOrderFullSequence:
    """Open order -> Cancel -> No must land back on the detail view, no crash."""

    async def test_open_cancel_no_round_trip_with_frozen_query(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        handler._handle_error = AsyncMock()

        order_id = 246
        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "order_details", lambda *_a, **_k: "details-kbd")
        rendered = {}

        def _yes_no(*_a, **kwargs):
            rendered.update(kwargs)
            return "yes-no-kbd"

        monkeypatch.setattr(orders_module.MenuKeyboards, "yes_no_buttons", _yes_no)
        api = RecordingAPIContext()
        monkeypatch.setattr(orders_module, "api_client", api)
        context = make_context()

        # 1) Open the order details via the '^order_' handler path (parses id).
        open_update = DummyUpdate()
        open_update.callback_query = DummyCallbackQuery(data=f"order_{order_id}")
        await handler.order_details(open_update, context)
        assert api.requested_ids == [order_id]
        open_update.callback_query.edit_message_text.assert_awaited_once()

        # 2) Tap "Cancel" -> confirmation prompt; cancelling id is stashed.
        cancel_update = DummyUpdate()
        cancel_update.callback_query = DummyCallbackQuery(data=f"cancel_order_{order_id}")
        await handler.cancel_order(cancel_update, context)
        # The id travels on the buttons, so the card still works after a deploy.
        assert rendered["no_callback"] == f"cancel_order_{order_id}_confirm_no"
        assert "cancelling_order_id" not in context.user_data
        cancel_update.callback_query.edit_message_text.assert_awaited_once_with(
            text="telegram.orders.cancel_confirm:en",
            reply_markup="yes-no-kbd",
        )

        # 3) Tap "No" with a FROZEN callback query (the prod-crash shape) —
        #    exactly the data the card just rendered.
        no_update = DummyUpdate()
        no_update.callback_query = FrozenCallbackQuery(data=rendered["no_callback"])
        await handler.cancel_order_confirm_no(no_update, context)

        # Back on the detail view for the same order, with no AttributeError.
        handler._handle_error.assert_not_awaited()
        assert no_update.callback_query.data == f"cancel_order_{order_id}_confirm_no"
        no_update.callback_query.edit_message_text.assert_awaited_once()
        assert "cancelling_order_id" not in context.user_data
        # The id flowed through unchanged from Cancel -> No -> details
        # (opened once, then re-opened after "No").
        assert api.requested_ids == [order_id, order_id]


# ---------------------------------------------------------------------------
# The Yes button after a deploy — driven through the real dispatcher
# ---------------------------------------------------------------------------
#
# The unit tests above call the handlers directly, so they can hand the id over
# in whatever way the test likes. The defect below is about what SURVIVES
# between two taps that a redeploy sits between, which only the real
# Application can answer: it builds the card, it routes the tap the card emits,
# and it carries none of the bot's memory across a restart because
# `WaterBusinessBot` builds the Application with no `persistence`.

ORDER_ID = 8421

CANCELLABLE_ORDER = {
    "id": ORDER_ID,
    "order_number": f"TG_{ORDER_ID}",
    "created_at": "2026-08-20T10:00:00",
    "total_amount": 42000,
    "status": "pending",
    "order_items": [],
}


@pytest.fixture
async def cancel_bot(monkeypatch):
    """A customer with one cancellable order, and a backend that records cancels."""
    harness = await build_bot_harness(monkeypatch)

    harness.cancelled: list[int] = []
    harness.backend.route(
        "GET", "/api/v1/orders/", lambda _c: {"data": {"orders": [CANCELLABLE_ORDER]}}
    )
    harness.backend.route(
        "GET",
        f"/api/v1/orders/{ORDER_ID}",
        lambda _c: {"data": {"order": CANCELLABLE_ORDER, "delivery": None}},
    )

    def _cancel(_call):
        harness.cancelled.append(ORDER_ID)
        return {"data": {"order": {**CANCELLABLE_ORDER, "status": "cancelled"}}}

    harness.backend.route("POST", f"/api/v1/orders/{ORDER_ID}/cancel", _cancel)
    return harness


@pytest.mark.integration
@pytest.mark.anyio
class TestCancelConfirmationSurvivesARestart:
    """The Yes/No card must keep working after the bot process is replaced."""

    async def test_yes_cancels_the_order_with_nothing_in_bot_memory(self, cancel_bot):
        """The card is tapped by a process that never rendered it.

        A deploy empties `context.user_data` — the Application has no
        `persistence` — while the card stays in the customer's chat. The id has
        to travel on the callback data or it is simply gone, which is what made
        "Yes" a dead button: the handler found no id, answered with an empty
        `query.answer()` and returned, so the card stayed on screen looking
        live and every later tap did exactly as little.
        """
        user = cancel_bot.updates()

        await cancel_bot.send(user.tap(f"cancel_order_{ORDER_ID}"))
        card = cancel_bot.telegram.last_shown()
        yes = next(data for data in card.callback_data() if data.endswith("_yes"))
        assert str(ORDER_ID) in yes, (
            f"the id must ride on the callback, not in bot memory: {card.callback_data()}"
        )

        # The deploy: the bot's memory of this customer is gone, and all that
        # is left is the card already sitting in their chat.
        restarted = cancel_bot
        restarted.application.drop_user_data(DEFAULT_USER_ID)
        restarted.application.drop_chat_data(DEFAULT_CHAT_ID)
        assert not restarted.application.user_data.get(DEFAULT_USER_ID)
        restarted.telegram.reset()

        await restarted.send(user.tap(yes))

        assert restarted.cancelled == [ORDER_ID], (
            "the tap did not reach the backend — the Yes button is dead again"
        )
        assert restarted.telegram.shown, "the customer was left on the dead card"

    async def test_no_returns_to_the_order_with_nothing_in_bot_memory(self, cancel_bot):
        """The other half of the card has to survive the same restart."""
        user = cancel_bot.updates()

        await cancel_bot.send(user.tap(f"cancel_order_{ORDER_ID}"))
        no = next(
            data for data in cancel_bot.telegram.last_shown().callback_data()
            if data.endswith("_no")
        )
        assert str(ORDER_ID) in no

        cancel_bot.application.drop_user_data(DEFAULT_USER_ID)
        cancel_bot.application.drop_chat_data(DEFAULT_CHAT_ID)
        cancel_bot.telegram.reset()

        await cancel_bot.send(user.tap(no))

        assert cancel_bot.cancelled == [], "'No' must not cancel anything"
        detail_calls = [
            c for c in cancel_bot.backend.calls
            if c.method == "GET" and c.endpoint == f"/api/v1/orders/{ORDER_ID}"
        ]
        assert detail_calls, "the customer was not taken back to their order"

    async def test_a_card_from_before_this_release_tells_the_customer(self, cancel_bot):
        """Cards already in customers' chats carry no id — say so, don't stall.

        `cancel_order_confirm_yes` (no id) is what the previous release
        rendered. It stays claimed by its registered handler, and the customer
        must end up somewhere alive rather than tapping a card that answers
        with nothing.
        """
        user = cancel_bot.updates()

        await cancel_bot.send(user.tap("cancel_order_confirm_yes"))

        assert cancel_bot.cancelled == [], "an id-less tap must never cancel an order"
        assert cancel_bot.telegram.shown, (
            "the customer was left on the dead card with no way to tell"
        )
        toasts = [c for c in cancel_bot.telegram.calls if c.method == "answerCallbackQuery"]
        assert any(c.params.get("text") for c in toasts), (
            "the tap was answered with nothing at all — exactly the dead button"
        )
