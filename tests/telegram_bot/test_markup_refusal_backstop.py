"""When Telegram refuses a screen's markup, the screen is shown as plain text and is never lost.

`BaseHandler._edit_or_replace_callback_message` used to answer ANY refused edit by
deleting the bubble and resending the SAME text with the SAME parse_mode. A markup
refusal is deterministic, so the resend failed too and the customer was left with no
screen at all (2026-10-18: the cart vanished three times). Now a "can't parse
entities" edit is retried as plain text before anything is deleted, and an
unavoidable replacement is SENT before the old bubble is deleted.

The screen is order tracking (legacy Markdown, raw copy) with an owner-edited title
carrying a stray asterisk. It is a translation, not data, so no escaping prevents it.
"""

from __future__ import annotations

import pytest

from handlers.base import BaseHandler
from handlers.profile import ProfileHandlers

from tests.telegram_bot.ptb_harness import build_bot_harness, menu_already_installed

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ORDER_ID = 1125
# "📍 *Buyurtma kuzatuvi**" closes the bold at byte 23 and leaves the '*' at byte 24 open.
TRACKING_TITLE = "Buyurtma kuzatuvi*"
PARSE_REFUSAL = "Bad Request: can't parse entities: Can't find end of the entity starting at byte offset 24"


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(
        monkeypatch,
        translations={("uz", "telegram.orders.tracking_title"): TRACKING_TITLE},
        strict_entities=True,
    )
    harness.backend.route(
        "GET", f"/api/v1/orders/{ORDER_ID}/track",
        lambda _call: {"data": {
            "order": {"id": ORDER_ID, "order_number": "TG_001125_26", "status": "pending"},
            "delivery": {},
            "timeline": [],
        }},
    )
    await menu_already_installed(harness)
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


def screen_calls(bot):
    """(method, parse_mode, delivered?) for every call that draws or removes a screen, in order."""
    return [
        (call.method, call.params.get("parse_mode"), call.refused is None)
        for call in bot.telegram.calls
        if call.method in {"editMessageText", "sendMessage", "deleteMessage"}
    ]


def error_toasts(bot):
    return [c.params.get("text") for c in bot.telegram.of("answerCallbackQuery") if c.params.get("text")]


async def test_a_refused_markup_edit_is_retried_as_plain_text_and_nothing_is_deleted(bot, user):
    await bot.send(user.tap(f"track_order_{ORDER_ID}"))

    assert screen_calls(bot) == [
        ("editMessageText", "Markdown", False),
        ("editMessageText", None, True),
    ]
    refused, shown = bot.telegram.of("editMessageText")
    assert refused.refused == PARSE_REFUSAL
    assert shown.text == refused.text, "the same words, only the formatting dropped"
    assert shown.callback_data() == [f"order_{ORDER_ID}", "menu_orders"]
    assert "Error occurred" not in error_toasts(bot)


async def test_an_unavoidable_replace_sends_the_new_screen_before_deleting_the_old(bot, user):
    bot.telegram.fail("editMessageText", "Bad Request: message to edit not found")

    await bot.send(user.tap(f"track_order_{ORDER_ID}"))

    assert screen_calls(bot) == [
        ("editMessageText", "Markdown", False),
        ("sendMessage", "Markdown", False),
        ("sendMessage", None, True),
        ("deleteMessage", None, True),
    ]
    assert bot.telegram.delivered[-1].callback_data() == [f"order_{ORDER_ID}", "menu_orders"]
    assert "Error occurred" not in error_toasts(bot)


async def test_a_replacement_that_cannot_be_sent_leaves_the_old_screen_in_place(bot, user):
    bot.telegram.fail("editMessageText", "Bad Request: message to edit not found")
    bot.telegram.fail("sendMessage", "Bad Request: message is too long")

    await bot.send(user.tap(f"track_order_{ORDER_ID}"))

    assert bot.telegram.of("deleteMessage") == [], "deleting before a replacement arrived is how the cart vanished"
    assert len(bot.telegram.of("sendMessage")) == 1, "a refusal that is not about markup is not retried as plain text"
    assert "Error occurred" in error_toasts(bot)


def test_one_plain_text_fallback_rule_lives_on_base_handler():
    assert "_reply_markdown_or_plain" not in vars(ProfileHandlers)
    assert ProfileHandlers._reply_markdown_or_plain is BaseHandler._reply_markdown_or_plain
