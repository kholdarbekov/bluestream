"""Tests that lock in the active-deliveries render lifecycle.

Two behaviours we never want to regress:

1. Tracked card message IDs from the previous render are deleted before the
   next render runs — so repeatedly tapping "Optimize routes" never stacks
   duplicate per-delivery cards in the chat.

2. The header is only edited when its content actually changed — so we never
   issue a no-op `edit_message_text` and trip Telegram's "Message is not
   modified" error.

These are pure-logic tests of the static helpers + light integration via a
mocked `context.user_data` and `context.bot.delete_message`. No real
Telegram API is touched. We use `asyncio.run` directly to avoid a
pytest-asyncio dependency in the staff_bot container.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from staff_bot.handlers.delivery.active_delivery import (
    ActiveDeliveryHandler,
    _CARDS_KEY,
    _HEADER_SIG_KEY,
)


def _ctx_with_cards(card_ids):
    """Build a minimal `context` stand-in: just `bot` (with awaitable
    delete_message) and `user_data`. No Flask, no full Application."""
    ctx = MagicMock()
    ctx.bot.delete_message = AsyncMock()
    ctx.user_data = {_CARDS_KEY: list(card_ids)}
    return ctx


# ---------------------------------------------------------------------------
# _compute_render_signature
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestComputeRenderSignature:
    def test_identical_text_and_keyboard_produce_identical_signature(self):
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Optimize", callback_data="staff_optimize_routes")]]
        )
        a = ActiveDeliveryHandler._compute_render_signature("hello", kb)
        b = ActiveDeliveryHandler._compute_render_signature("hello", kb)
        assert a == b

    def test_different_text_produces_different_signature(self):
        kb = InlineKeyboardMarkup([])
        a = ActiveDeliveryHandler._compute_render_signature("hello", kb)
        b = ActiveDeliveryHandler._compute_render_signature("HELLO", kb)
        assert a != b

    def test_different_button_label_produces_different_signature(self):
        a = ActiveDeliveryHandler._compute_render_signature(
            "hello",
            InlineKeyboardMarkup([[InlineKeyboardButton("Optimize", callback_data="x")]]),
        )
        b = ActiveDeliveryHandler._compute_render_signature(
            "hello",
            InlineKeyboardMarkup([[InlineKeyboardButton("Reoptimize", callback_data="x")]]),
        )
        assert a != b

    def test_different_callback_data_produces_different_signature(self):
        a = ActiveDeliveryHandler._compute_render_signature(
            "hello",
            InlineKeyboardMarkup([[InlineKeyboardButton("X", callback_data="cb_a")]]),
        )
        b = ActiveDeliveryHandler._compute_render_signature(
            "hello",
            InlineKeyboardMarkup([[InlineKeyboardButton("X", callback_data="cb_b")]]),
        )
        assert a != b

    def test_no_keyboard_does_not_crash(self):
        sig = ActiveDeliveryHandler._compute_render_signature("hello", None)
        assert isinstance(sig, str)
        assert len(sig) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# _delete_previous_card_messages
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeletePreviousCards:
    def test_deletes_each_tracked_card_then_clears_list(self):
        ctx = _ctx_with_cards([(100, 1), (100, 2), (100, 3)])

        asyncio.run(ActiveDeliveryHandler._delete_previous_card_messages(ctx))

        # All three IDs were attempted for deletion in order.
        assert ctx.bot.delete_message.call_count == 3
        called_ids = [
            (call.kwargs["chat_id"], call.kwargs["message_id"])
            for call in ctx.bot.delete_message.call_args_list
        ]
        assert called_ids == [(100, 1), (100, 2), (100, 3)]

        # State cleared so the next render starts from a clean slate.
        assert ctx.user_data[_CARDS_KEY] == []

    def test_swallows_per_message_failures_and_keeps_going(self):
        """If one delete fails (already deleted, too old, perms changed),
        we must still attempt the rest and clear the tracking list."""
        ctx = _ctx_with_cards([(100, 1), (100, 2), (100, 3)])

        # Second call raises; first and third succeed.
        ctx.bot.delete_message.side_effect = [None, RuntimeError("gone"), None]

        asyncio.run(ActiveDeliveryHandler._delete_previous_card_messages(ctx))

        assert ctx.bot.delete_message.call_count == 3
        assert ctx.user_data[_CARDS_KEY] == []

    def test_no_op_when_nothing_tracked(self):
        ctx = MagicMock()
        ctx.bot.delete_message = AsyncMock()
        ctx.user_data = {}

        asyncio.run(ActiveDeliveryHandler._delete_previous_card_messages(ctx))

        ctx.bot.delete_message.assert_not_called()
        # Even with no prior key, the post-condition is an empty list.
        assert ctx.user_data[_CARDS_KEY] == []

    def test_no_op_when_tracked_list_is_none(self):
        ctx = MagicMock()
        ctx.bot.delete_message = AsyncMock()
        ctx.user_data = {_CARDS_KEY: None}

        asyncio.run(ActiveDeliveryHandler._delete_previous_card_messages(ctx))

        ctx.bot.delete_message.assert_not_called()
        assert ctx.user_data[_CARDS_KEY] == []


# ---------------------------------------------------------------------------
# _render_header — signature-aware edit avoidance
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRenderHeader:
    def test_callback_skips_edit_when_signature_unchanged(self):
        """The whole point of the signature: if nothing changed since the
        last render, never call `edit_message_text`. Telegram rejects no-op
        edits with `BadRequest: Message is not modified` and we never want
        to see that error in the logs."""
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Optimize", callback_data="staff_optimize_routes")]]
        )
        sig = ActiveDeliveryHandler._compute_render_signature("hello", kb)

        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.edit_message_text = AsyncMock()

        ctx = MagicMock()
        ctx.user_data = {_HEADER_SIG_KEY: sig}

        handler = ActiveDeliveryHandler.__new__(ActiveDeliveryHandler)
        asyncio.run(handler._render_header(update, ctx, "hello", kb))

        update.callback_query.edit_message_text.assert_not_called()
        # Stored signature unchanged.
        assert ctx.user_data[_HEADER_SIG_KEY] == sig

    def test_callback_edits_when_signature_changed(self):
        kb = InlineKeyboardMarkup([])
        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.edit_message_text = AsyncMock()

        ctx = MagicMock()
        ctx.user_data = {_HEADER_SIG_KEY: "stale-signature"}

        handler = ActiveDeliveryHandler.__new__(ActiveDeliveryHandler)
        asyncio.run(handler._render_header(update, ctx, "fresh content", kb))

        update.callback_query.edit_message_text.assert_awaited_once()
        # Signature was refreshed to the new content's hash.
        new_sig = ActiveDeliveryHandler._compute_render_signature("fresh content", kb)
        assert ctx.user_data[_HEADER_SIG_KEY] == new_sig

    def test_non_callback_always_sends_a_new_message(self):
        kb = InlineKeyboardMarkup([])
        update = MagicMock()
        update.callback_query = None
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()

        ctx = MagicMock()
        ctx.user_data = {_HEADER_SIG_KEY: "any"}

        handler = ActiveDeliveryHandler.__new__(ActiveDeliveryHandler)
        asyncio.run(handler._render_header(update, ctx, "hello", kb))

        update.message.reply_text.assert_awaited_once()
        # Signature stored even when sending a fresh message — so the next
        # callback edit can compare against it.
        assert ctx.user_data[_HEADER_SIG_KEY] == ActiveDeliveryHandler._compute_render_signature(
            "hello", kb
        )
