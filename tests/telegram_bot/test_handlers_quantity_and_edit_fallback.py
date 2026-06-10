"""Regression coverage for the qty_current IndexError and the callback edit fallback.

Prod triage 2026-06-10: tapping the quantity display button (callback_data
'qty_current', routed by the broad '^qty_' pattern) crashed quantity_handler
with 'list index out of range', and _edit_or_replace_callback_message both
spammed warnings for expected media-message fallbacks and delete+resent
identical messages on 'Message is not modified'.
"""

import logging
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from handlers import products as products_module
from handlers.base import BaseHandler
from tests.telegram_bot.helpers import (
    DummyCallbackQuery,
    DummyUpdate,
    FakeAPIClientContext,
    make_context,
)


def _i18n_get(key, language, *args, **kwargs):
    return f"{key}:{language}"


@pytest.mark.unit
@pytest.mark.anyio
class TestQuantityHandlerCallbackParsing:
    async def test_qty_current_is_noop(self, monkeypatch):
        """The quantity display button must answer the callback and do nothing else."""
        handler = products_module.ProductHandlers()
        handler._handle_error = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="qty_current")
        context = make_context()
        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "api_client", FakeAPIClientContext())

        await handler.quantity_handler(update, context)

        update.callback_query.answer.assert_awaited_once()
        handler._handle_error.assert_not_awaited()

    async def test_malformed_qty_callback_answers_invalid_action(self, monkeypatch):
        """Short/unknown qty_* payloads must short-circuit instead of crashing."""
        handler = products_module.ProductHandlers()
        handler._handle_error = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="qty_bogus")
        context = make_context()
        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "api_client", FakeAPIClientContext())

        await handler.quantity_handler(update, context)

        update.callback_query.answer.assert_awaited_once_with("telegram.products.invalid_action:en")
        handler._handle_error.assert_not_awaited()

    async def test_quantity_handler_passes_exception_context(self, monkeypatch):
        """Real failures must reach _handle_error with exc= so the log has a traceback."""
        handler = products_module.ProductHandlers()
        handler._handle_error = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="qty_inc_9_1")
        context = make_context()
        boom = RuntimeError("boom")
        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(side_effect=boom))
        monkeypatch.setattr(products_module, "api_client", FakeAPIClientContext())

        await handler.quantity_handler(update, context)

        handler._handle_error.assert_awaited_once()
        kwargs = handler._handle_error.await_args.kwargs
        assert kwargs.get("exc") is boom
        assert kwargs.get("operation") == "quantity_handler"


@pytest.mark.unit
@pytest.mark.anyio
class TestEditOrReplaceCallbackMessage:
    async def test_not_modified_returns_without_replacing(self):
        """'Message is not modified' means the UI is already correct: no delete+resend."""
        handler = BaseHandler()
        query = DummyCallbackQuery()
        query.edit_message_text = AsyncMock(
            side_effect=BadRequest(
                "Message is not modified: specified new message content and reply "
                "markup are exactly the same as a current content and reply markup "
                "of the message"
            )
        )

        await handler._edit_or_replace_callback_message(query, "same text")

        query.message.delete.assert_not_awaited()
        query.message.reply_text.assert_not_awaited()

    async def test_media_message_falls_back_with_info_log(self, caplog):
        """Editing a media message can't work; the replace fallback is expected and
        must not log at WARNING."""
        handler = BaseHandler()
        query = DummyCallbackQuery()
        query.edit_message_text = AsyncMock(
            side_effect=BadRequest("There is no text in the message to edit")
        )

        with caplog.at_level(logging.INFO, logger="handlers.base"):
            await handler._edit_or_replace_callback_message(query, "new text")

        query.message.delete.assert_awaited_once()
        query.message.reply_text.assert_awaited_once_with(text="new text")
        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not warning_records

    async def test_unexpected_edit_failure_still_warns_and_replaces(self, caplog):
        """Non-BadRequest edit failures keep the replace fallback and the WARNING."""
        handler = BaseHandler()
        query = DummyCallbackQuery()
        query.edit_message_text = AsyncMock(side_effect=TimeoutError("slow network"))

        with caplog.at_level(logging.INFO, logger="handlers.base"):
            await handler._edit_or_replace_callback_message(query, "new text")

        query.message.delete.assert_awaited_once()
        query.message.reply_text.assert_awaited_once_with(text="new text")
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    async def test_without_message_reraises_the_edit_error(self):
        """No message to fall back to: the original edit exception must propagate
        (the old bare `raise` outside the except block raised RuntimeError)."""
        handler = BaseHandler()
        query = DummyCallbackQuery()
        query.edit_message_text = AsyncMock(side_effect=BadRequest("Chat not found"))
        query.message = None

        with pytest.raises(BadRequest):
            await handler._edit_or_replace_callback_message(query, "text")
