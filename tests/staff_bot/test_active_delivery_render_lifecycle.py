"""Pins the one piece of the old render lifecycle that survives Task 6:
`_compute_render_signature`, the SSOT delegation used by the route card's
edit-vs-no-op comparison. The delete-and-resend machinery this file used to
also cover (`_delete_previous_card_messages`, `_render_header`, and the
`_CARDS_KEY`/`_HEADER_KEY`/`_HEADER_SIG_KEY` tracking) was deleted in
Task 6 — its job now lives in `staff_bot.handlers.delivery.route_card`
(Task 3/5), covered by `test_route_card_render.py` and
`test_route_card_state.py`.

We use `asyncio.run` directly to avoid a pytest-asyncio dependency in the
staff_bot container.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import pytest

from staff_bot.handlers.delivery.active_delivery import ActiveDeliveryHandler


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
