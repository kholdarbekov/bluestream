"""Editing a message you already sent must not crash the bot.

WHY THIS FILE EXISTS
--------------------
The customer bot's top-level text catch-all is registered as

    MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text_message)

and its first real statement was ``text = update.message.text.strip()``.

``filters.TEXT`` matches an EDITED message. PTB's ``MessageFilter.check_update``
(verified against the installed 22.3) hands ``update.effective_message`` to the
filter, and ``effective_message`` resolves to ``edited_message`` when that is
what arrived. ``run_polling`` is called with ``allowed_updates=None`` — "accept
all update types" — so these updates really are delivered.

On an edit ``update.message`` is ``None``, so the router raised
``AttributeError``. Its own ``except`` then did
``await update.message.reply_text(...)`` and raised a SECOND ``AttributeError``
from inside the handler, so the failure escaped to the global error handler and
the customer saw nothing at all. ``grep -c effective_message telegram_bot/bot.py``
was zero.

A customer fixing a typo in the street name they just typed is enough to trigger
it. No restart, no stale card, no conversation state required.

The fix is at the registration layer rather than inside the handler: an edit is
not new input, and re-running the catch-all over one would re-file an already
captured support message and re-consume an OTP that was already spent.
"""

import pytest

from tests.telegram_bot.ptb_harness import build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


TRANSLATIONS = {
    "telegram.error_occurred": "SOMETHING-WENT-WRONG",
}


@pytest.fixture
async def bot(monkeypatch):
    return await build_bot_harness(monkeypatch, translations=TRANSLATIONS)


@pytest.fixture
def user(bot):
    return bot.updates()


def collect_errors(bot):
    errors = []
    bot.application.add_error_handler(
        lambda _update, context: errors.append(context.error) or None
    )
    return errors


async def test_editing_a_message_does_not_crash_the_text_router(bot, user):
    """The seed: `update.message` is None on an edit, and the except branch
    dereferenced it a second time."""
    errors = collect_errors(bot)

    await bot.send(user.edited_text("Amir Temur 15"))

    assert not errors, (
        f"editing a message still raises: {errors!r}. `update.message` is None "
        "on an edited_message; use `effective_message` or exclude edits."
    )


async def test_editing_a_message_is_not_filed_a_second_time_as_support(bot, user):
    """The original text was already captured when it was first sent. Running
    the catch-all again over the edit would post a duplicate support row."""
    await bot.send(user.text("my delivery never arrived"))
    support_calls_before = [
        call for call in bot.backend.calls
        if call.endpoint == "/api/v1/support/messages"
    ]

    await bot.send(user.edited_text("my delivery never arrived!!"))

    support_calls_after = [
        call for call in bot.backend.calls
        if call.endpoint == "/api/v1/support/messages"
    ]
    assert support_calls_after == support_calls_before, (
        "the edit was filed as a second support message"
    )


async def test_a_normal_text_message_still_reaches_the_router(bot, user):
    """The guard must exclude edits only. A plain message is still captured —
    otherwise the fix silently kills the Support Inbox."""
    await bot.send(user.text("my delivery never arrived"))

    assert any(
        call.endpoint == "/api/v1/support/messages" for call in bot.backend.calls
    ), "a normal text message stopped reaching the support capture"
