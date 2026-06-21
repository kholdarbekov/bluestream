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
        update.callback_query = FrozenCallbackQuery(data="cancel_order_confirm_no")
        context = make_context()
        context.user_data["cancelling_order_id"] = 42

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))

        await handler.cancel_order_confirm_no(update, context)

        # Re-dispatches with the id passed EXPLICITLY (the fix), not via data.
        handler.order_details.assert_awaited_once_with(update, context, order_id=42)
        # Frozen callback data is untouched.
        assert update.callback_query.data == "cancel_order_confirm_no"
        # The immutable-attribute crash never reached the error handler.
        handler._handle_error.assert_not_awaited()
        # Cancellation context is cleared so a stale id can't leak into the next flow.
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
        update.callback_query = FrozenCallbackQuery(data="cancel_order_confirm_no")
        context = make_context()
        context.user_data["cancelling_order_id"] = 314

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
        assert update.callback_query.data == "cancel_order_confirm_no"
        handler._handle_error.assert_not_awaited()
        assert "cancelling_order_id" not in context.user_data

    async def test_no_cancelling_order_id_just_answers_and_returns(self, monkeypatch):
        """Stale / expired tap with no cancelling_order_id: answer + bail, no work."""
        handler = orders_module.OrderHandlers()
        handler.order_details = AsyncMock()
        handler._handle_error = AsyncMock()

        update = DummyUpdate()
        update.callback_query = FrozenCallbackQuery(data="cancel_order_confirm_no")
        context = make_context()  # no cancelling_order_id

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))

        await handler.cancel_order_confirm_no(update, context)

        update.callback_query.answer.assert_awaited_once()
        handler.order_details.assert_not_awaited()
        handler._handle_error.assert_not_awaited()

    async def test_zero_cancelling_order_id_is_treated_as_absent(self, monkeypatch):
        """A falsy (0) order id must not re-dispatch into order_details."""
        handler = orders_module.OrderHandlers()
        handler.order_details = AsyncMock()
        handler._handle_error = AsyncMock()

        update = DummyUpdate()
        update.callback_query = FrozenCallbackQuery(data="cancel_order_confirm_no")
        context = make_context()
        context.user_data["cancelling_order_id"] = 0

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))

        await handler.cancel_order_confirm_no(update, context)

        update.callback_query.answer.assert_awaited_once()
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
        update.callback_query = FrozenCallbackQuery(data="cancel_order_confirm_no")
        context = make_context()
        context.user_data["cancelling_order_id"] = 77

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
        update.callback_query = FrozenCallbackQuery(data="cancel_order_confirm_no")
        context = make_context()
        context.user_data["cancelling_order_id"] = 5

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
        monkeypatch.setattr(orders_module.MenuKeyboards, "yes_no_buttons", lambda *_a, **_k: "yes-no-kbd")
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
        assert context.user_data["cancelling_order_id"] == order_id
        cancel_update.callback_query.edit_message_text.assert_awaited_once_with(
            text="telegram.orders.cancel_confirm:en",
            reply_markup="yes-no-kbd",
        )

        # 3) Tap "No" with a FROZEN callback query (the prod-crash shape).
        no_update = DummyUpdate()
        no_update.callback_query = FrozenCallbackQuery(data="cancel_order_confirm_no")
        await handler.cancel_order_confirm_no(no_update, context)

        # Back on the detail view for the same order, with no AttributeError.
        handler._handle_error.assert_not_awaited()
        assert no_update.callback_query.data == "cancel_order_confirm_no"
        no_update.callback_query.edit_message_text.assert_awaited_once()
        assert "cancelling_order_id" not in context.user_data
        # The id flowed through unchanged from Cancel -> No -> details
        # (opened once, then re-opened after "No").
        assert api.requested_ids == [order_id, order_id]
