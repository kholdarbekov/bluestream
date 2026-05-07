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
    _HEADER_KEY,
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


def _callback_update(chat_id: int = 100, source_msg_id: int = 7777):
    """Build a callback-style `update` mock whose source message lives in
    ``chat_id`` (so it can be compared against a tracked header's chat).

    ``source_msg_id`` defaults to a value distinct from the header ids used
    in the tests below, so accidental "wrong message" edits surface as
    assertion failures instead of silent passes.
    """
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.message = MagicMock()
    update.callback_query.message.chat = MagicMock()
    update.callback_query.message.chat.id = chat_id
    update.callback_query.message.message_id = source_msg_id
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update


def _ctx_with_bot():
    """Build a `context` whose `bot` exposes awaitable edit/delete shims."""
    ctx = MagicMock()
    ctx.bot.edit_message_text = AsyncMock()
    ctx.bot.delete_message = AsyncMock()
    ctx.user_data = {}
    return ctx


@pytest.mark.unit
class TestRenderHeader:
    def test_callback_skips_edit_when_signature_unchanged(self):
        """The whole point of the signature: if nothing changed since the
        last render, never call `edit_message_text`. Telegram rejects no-op
        edits with `BadRequest: Message is not modified` and we never want
        to see that error in the logs.

        The skip-path requires a tracked header in the same chat so we
        know there's actually something on screen we'd be skipping the
        update for — first callbacks with no tracked header always render
        fresh.
        """
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Optimize", callback_data="staff_optimize_routes")]]
        )
        sig = ActiveDeliveryHandler._compute_render_signature("hello", kb)

        update = _callback_update(chat_id=100)
        ctx = _ctx_with_bot()
        ctx.user_data = {
            _HEADER_SIG_KEY: sig,
            _HEADER_KEY: (100, 4242),  # tracked header in same chat
        }

        handler = ActiveDeliveryHandler.__new__(ActiveDeliveryHandler)
        asyncio.run(handler._render_header(update, ctx, "hello", kb))

        ctx.bot.edit_message_text.assert_not_called()
        update.callback_query.message.reply_text.assert_not_called()
        # Stored signature unchanged.
        assert ctx.user_data[_HEADER_SIG_KEY] == sig
        assert ctx.user_data[_HEADER_KEY] == (100, 4242)

    def test_callback_edits_tracked_header_not_callback_source(self):
        """Regression for "Message to edit not found":

        When the driver taps ⬅️ Back inside a delivery-detail view, the
        callback's source message is the *card-turned-detail* — already
        tracked in `_CARDS_KEY` and about to be deleted by
        `_delete_previous_card_messages` at the start of the next render.
        We must edit the separately tracked **header** message, not the
        callback's (soon-to-be-deleted) source.
        """
        kb = InlineKeyboardMarkup([])
        update = _callback_update(chat_id=100, source_msg_id=7777)

        ctx = _ctx_with_bot()
        ctx.user_data = {
            _HEADER_SIG_KEY: "stale-signature",
            _HEADER_KEY: (100, 4242),  # the real header, not the source
            # Source is a tracked card — modelling the back-from-detail
            # path. `_delete_previous_card_messages` would remove this id
            # *before* `_render_header` runs in the real flow.
            _CARDS_KEY: [(100, 7777)],
        }

        handler = ActiveDeliveryHandler.__new__(ActiveDeliveryHandler)
        asyncio.run(handler._render_header(update, ctx, "fresh content", kb))

        ctx.bot.edit_message_text.assert_awaited_once()
        kwargs = ctx.bot.edit_message_text.await_args.kwargs
        assert kwargs["chat_id"] == 100
        assert kwargs["message_id"] == 4242  # the tracked header — never 7777
        # Old API surface must not be reached either.
        update.callback_query.edit_message_text.assert_not_called()
        update.callback_query.message.reply_text.assert_not_called()
        # Signature was refreshed to the new content's hash.
        new_sig = ActiveDeliveryHandler._compute_render_signature("fresh content", kb)
        assert ctx.user_data[_HEADER_SIG_KEY] == new_sig
        assert ctx.user_data[_HEADER_KEY] == (100, 4242)

    def test_callback_falls_back_to_fresh_when_tracked_header_edit_fails(self):
        """Tracked header gone (deleted, too old, bot lost permission) →
        send a fresh header in the same chat and re-track its id. The
        debug log is enough; we don't surface this as an error because it
        is an expected recovery path."""
        kb = InlineKeyboardMarkup([])
        update = _callback_update(chat_id=100, source_msg_id=7777)
        # Make the new send return a Message-shaped mock so we can assert
        # the resulting (chat_id, message_id) is re-tracked.
        sent = MagicMock()
        sent.chat_id = 100
        sent.message_id = 9001
        update.callback_query.message.reply_text = AsyncMock(return_value=sent)

        ctx = _ctx_with_bot()
        ctx.bot.edit_message_text.side_effect = RuntimeError(
            "Message to edit not found"
        )
        ctx.user_data = {
            _HEADER_SIG_KEY: "old",
            _HEADER_KEY: (100, 4242),
        }

        handler = ActiveDeliveryHandler.__new__(ActiveDeliveryHandler)
        asyncio.run(handler._render_header(update, ctx, "after-recovery", kb))

        # We tried to edit, it failed, we sent fresh.
        ctx.bot.edit_message_text.assert_awaited_once()
        update.callback_query.message.reply_text.assert_awaited_once()
        # And re-tracked the new header for the next render.
        assert ctx.user_data[_HEADER_KEY] == (100, 9001)
        new_sig = ActiveDeliveryHandler._compute_render_signature("after-recovery", kb)
        assert ctx.user_data[_HEADER_SIG_KEY] == new_sig

    def test_callback_sends_fresh_when_no_tracked_header(self):
        """First callback after a context reset (or a session in a chat
        we don't have a tracked header for) → send a fresh header
        instead of editing some other message."""
        kb = InlineKeyboardMarkup([])
        update = _callback_update(chat_id=100, source_msg_id=7777)
        sent = MagicMock()
        sent.chat_id = 100
        sent.message_id = 5555
        update.callback_query.message.reply_text = AsyncMock(return_value=sent)

        ctx = _ctx_with_bot()
        ctx.user_data = {}  # no _HEADER_KEY, no signature

        handler = ActiveDeliveryHandler.__new__(ActiveDeliveryHandler)
        asyncio.run(handler._render_header(update, ctx, "hello", kb))

        ctx.bot.edit_message_text.assert_not_called()
        update.callback_query.message.reply_text.assert_awaited_once()
        assert ctx.user_data[_HEADER_KEY] == (100, 5555)

    def test_callback_sends_fresh_when_tracked_header_in_different_chat(self):
        """Defensive: if the tracked header lives in a different chat
        than the current callback (cross-chat dispatcher mistake, or a
        future group-chat handler), don't try to edit across chats —
        send a fresh header in the current chat."""
        kb = InlineKeyboardMarkup([])
        update = _callback_update(chat_id=100, source_msg_id=7777)
        sent = MagicMock()
        sent.chat_id = 100
        sent.message_id = 6666
        update.callback_query.message.reply_text = AsyncMock(return_value=sent)

        ctx = _ctx_with_bot()
        ctx.user_data = {
            _HEADER_KEY: (999, 4242),  # different chat
            _HEADER_SIG_KEY: "irrelevant",
        }

        handler = ActiveDeliveryHandler.__new__(ActiveDeliveryHandler)
        asyncio.run(handler._render_header(update, ctx, "hello", kb))

        ctx.bot.edit_message_text.assert_not_called()
        update.callback_query.message.reply_text.assert_awaited_once()
        assert ctx.user_data[_HEADER_KEY] == (100, 6666)

    def test_non_callback_always_sends_a_new_message(self):
        kb = InlineKeyboardMarkup([])
        update = MagicMock()
        update.callback_query = None
        update.message = MagicMock()
        sent = MagicMock()
        sent.chat_id = 100
        sent.message_id = 8888
        update.message.reply_text = AsyncMock(return_value=sent)

        ctx = _ctx_with_bot()
        ctx.user_data = {
            _HEADER_SIG_KEY: "any",
            # Stale tracked header from a previous flow — text-entry must
            # overwrite it, not edit it.
            _HEADER_KEY: (100, 1111),
        }

        handler = ActiveDeliveryHandler.__new__(ActiveDeliveryHandler)
        asyncio.run(handler._render_header(update, ctx, "hello", kb))

        update.message.reply_text.assert_awaited_once()
        ctx.bot.edit_message_text.assert_not_called()
        # Signature stored even when sending a fresh message — so the next
        # callback edit can compare against it.
        assert ctx.user_data[_HEADER_SIG_KEY] == ActiveDeliveryHandler._compute_render_signature(
            "hello", kb
        )
        # Tracked header replaced with the freshly sent one.
        assert ctx.user_data[_HEADER_KEY] == (100, 8888)
