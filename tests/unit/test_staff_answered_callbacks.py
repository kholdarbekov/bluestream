"""StaffExtBot remembers which taps were answered; only it can say so."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Chat, InaccessibleMessage, Message
from telegram.error import BadRequest

from staff_bot.handlers.base import BaseHandler
from staff_bot.utils.answered_callbacks import (
    ANSWERED_CALLBACK_IDS_MAX,
    StaffExtBot,
    build_staff_bot,
    callback_already_answered,
)
from tests.bot_dispatcher_harness import FakeTelegramTransport

pytestmark = pytest.mark.anyio


def _bot():
    telegram = FakeTelegramTransport()
    return build_staff_bot("424242:STAFF-TEST-TOKEN", request=telegram, get_updates_request=telegram), telegram


async def test_an_answered_tap_is_remembered_and_a_fresh_one_is_not():
    bot, _telegram = _bot()
    assert not bot.was_answered("cb1")
    await bot.answer_callback_query("cb1")
    assert bot.was_answered("cb1")
    assert not bot.was_answered("cb2")


async def test_a_refused_answer_still_spends_the_slot():
    """Telegram refusing ("query is too old") leaves no popup to show either."""
    bot, telegram = _bot()
    telegram.fail("answerCallbackQuery", "Bad Request: query is too old")
    with pytest.raises(BadRequest):
        await bot.answer_callback_query("cb9", text="x")
    assert bot.was_answered("cb9")


async def test_memory_is_bounded():
    bot, _telegram = _bot()
    for index in range(ANSWERED_CALLBACK_IDS_MAX + 5):
        bot._remember(f"cb{index}")
    assert not bot.was_answered("cb0")
    assert bot.was_answered(f"cb{ANSWERED_CALLBACK_IDS_MAX + 4}")


def test_a_bot_that_is_not_ours_reads_as_unanswered():
    """MagicMock queries (unit tests) and foreign bots keep today's behaviour."""
    query = MagicMock()
    assert callback_already_answered(query) is False


def test_a_query_without_a_bot_reads_as_unanswered():
    query = MagicMock()
    query.get_bot.side_effect = RuntimeError("no bot")
    assert callback_already_answered(query) is False


def test_build_staff_bot_returns_the_recording_class():
    bot, _telegram = _bot()
    assert isinstance(bot, StaffExtBot)


def _handler():
    return BaseHandler.__new__(BaseHandler)


def _update_with_query():
    update = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.message = MagicMock(spec=Message)
    update.callback_query.message.reply_text = AsyncMock()
    update.effective_chat.send_message = AsyncMock()
    return update


async def test_an_unanswered_tap_still_gets_a_popup_and_no_message():
    update = _update_with_query()
    await _handler()._notify_user(update, "❌ short", show_alert=True)
    update.callback_query.answer.assert_awaited_once_with("❌ short", show_alert=True)
    update.callback_query.message.reply_text.assert_not_awaited()


async def test_copy_over_the_popup_limit_goes_to_the_chat_and_still_stops_the_spinner():
    update = _update_with_query()
    long_text = "❌ " + "x" * BaseHandler.TELEGRAM_ALERT_MAX_CHARS
    await _handler()._notify_user(update, long_text, show_alert=True)
    update.callback_query.answer.assert_awaited_once_with(None, show_alert=False)
    # Silent, like every other driver-facing message the bot sends (staff_bot/bot.py).
    update.callback_query.message.reply_text.assert_awaited_once_with(
        long_text, reply_markup=None, disable_notification=True
    )
    update.effective_chat.send_message.assert_not_awaited()


def _query_without_its_message(kind):
    """A tap whose message the bot cannot reply to: Telegram omits it (None) or
    sends an InaccessibleMessage (deleted, or otherwise inaccessible to the
    bot), which has no ``reply_text`` in PTB 22."""
    update = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.message = (
        None if kind == "missing" else InaccessibleMessage(chat=Chat(4242, Chat.PRIVATE), message_id=7)
    )
    update.effective_chat.send_message = AsyncMock()
    return update


@pytest.mark.parametrize("kind", ["missing", "inaccessible"])
async def test_an_error_on_a_tap_without_its_message_still_reaches_the_chat(kind):
    update = _query_without_its_message(kind)
    long_text = "❌ " + "x" * BaseHandler.TELEGRAM_ALERT_MAX_CHARS
    await _handler()._notify_user(update, long_text, show_alert=True)
    update.effective_chat.send_message.assert_awaited_once_with(
        long_text, reply_markup=None, disable_notification=True
    )
