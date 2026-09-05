"""Three audit findings that turned out NOT to reproduce — pinned so they stay
that way.

WHY THIS FILE EXISTS
--------------------
The 2026-09-03 audit filed three defects here. Driving them showed the code is
already correct, and in two cases the finding rested on a premise the audit
itself later disproved. Rather than "fix" nothing and call them closed, the
properties that make them non-defects are asserted here, because each is one
edit away from becoming true.

* ``start_order_issue_report`` / ``cancel_issue_report`` were filed as dead
  after a restart, on the theory that the tap is queued during downtime and
  redelivered too old to answer. It is not: ``drop_pending_updates`` defaults to
  ``true`` and is set in no ``.env`` or compose file, so the backlog is
  DISCARDED — the tap never arrives at all. And both callbacks are registered
  TOP LEVEL, not inside a conversation, so a tap made after the restart lands
  normally. What is worth pinning is that they stay top-level, and that the
  arming they leave in ``users.bot_state`` stays time-bounded — an unbounded one
  would file the customer's next unrelated message as their issue report.

* ``show_pickup_overview`` was filed as showing try-out A's products under
  try-out B's card. It reads its task id from the CALLBACK
  (``staff_tryout_pickup_back_<task_id>``) and re-fetches that task, so it is
  anchored on the card that was actually tapped. The id must keep riding the
  callback; the day it comes from ``user_data`` instead, the filed bug becomes
  real. That one is pinned in tests/staff_bot/.
"""

import pytest
from telegram.ext import CallbackQueryHandler, ConversationHandler

from handlers.support import _SUPPORT_STALE_MINUTES, SupportHandlers

from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
async def bot(monkeypatch):
    return await build_bot_harness(monkeypatch)


@pytest.fixture
def user(bot):
    return bot.updates()


def restart(bot):
    bot.application.user_data[DEFAULT_USER_ID].clear()
    for group in bot.application.handlers.values():
        for handler in group:
            conversations = getattr(handler, "_conversations", None)
            if conversations is not None:
                conversations.clear()
    bot.telegram.reset()


def acting_handlers(bot, update):
    return [
        (group, handler)
        for group, handler in bot.handlers_matching(update)
        if getattr(getattr(handler, "callback", None), "__name__", "")
        != "debug_callback_handler"
    ]


@pytest.mark.parametrize("callback", ["report_issue_555", "support_cancel"])
async def test_the_support_buttons_are_claimed_after_a_restart(bot, user, callback):
    """Both are top-level, so unlike the address-flow set they never depended on
    a conversation surviving the process."""
    restart(bot)

    assert acting_handlers(bot, user.tap(callback)), (
        f"{callback!r} is no longer claimed once conversations are gone — it "
        "has been moved into a conversation state"
    )


@pytest.mark.parametrize("callback", ["report_issue_555", "support_cancel"])
async def test_the_support_buttons_are_not_inside_a_conversation(bot, callback):
    """The structural half of the same claim, stated where a future edit would
    have to notice it."""
    inside_conversations = []
    for group in bot.application.handlers.values():
        for handler in group:
            if not isinstance(handler, ConversationHandler):
                continue
            for state_handlers in handler.states.values():
                for inner in state_handlers:
                    if not isinstance(inner, CallbackQueryHandler):
                        continue
                    if inner.pattern is not None and inner.pattern.match(callback):
                        inside_conversations.append(handler.name)

    assert not inside_conversations, (
        f"{callback!r} is now claimed inside {inside_conversations}, so it dies "
        "with the process the way the address-flow buttons did"
    )


def test_the_issue_report_arming_is_time_bounded():
    """Without a window, an arming that outlives a dropped Cancel would file the
    customer's next unrelated message as their issue report — for good."""
    assert _SUPPORT_STALE_MINUTES > 0, "the concern arming has no expiry at all"
    assert SupportHandlers._is_stale(None), (
        "an arming with no timestamp must count as stale, or a malformed row "
        "arms the flow permanently"
    )
