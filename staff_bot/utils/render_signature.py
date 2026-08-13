"""Stable content hash for Telegram message renders.

Telegram rejects no-op edits with `BadRequest: Message is not modified`;
comparing signatures across renders lets callers skip identical edits.
Extracted from ActiveDeliveryHandler._compute_render_signature so the route
card (rendered from BOTH the PTB handlers and the webhook server) can reuse
it without importing a handler class.
"""
import hashlib
import json

from telegram import InlineKeyboardMarkup


def compute_render_signature(text: str, keyboard) -> str:
    kb_repr = []
    if isinstance(keyboard, InlineKeyboardMarkup):
        for row in keyboard.inline_keyboard:
            for btn in row:
                kb_repr.append(
                    f"{getattr(btn, 'text', '')}|"
                    f"{getattr(btn, 'callback_data', '') or ''}|"
                    f"{getattr(btn, 'url', '') or ''}"
                )
    payload = text + '||' + json.dumps(kb_repr)
    return hashlib.sha256(payload.encode()).hexdigest()
