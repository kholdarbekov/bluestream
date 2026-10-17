"""The customer bot reports blocks and unblocks to the backend (spec 2026-10-09 §7.2 a, b; D8, D19).

Driven through the REAL dispatcher (tests/telegram_bot/ptb_harness.py): the update goes in through
`process_update`, so the handler group, the middleware group and the real `api_client` wrapper are
all in the loop. The backend seam records whether the call was signed (`sign=True`).
"""

from datetime import UTC, datetime

import pytest
from telegram.ext import ChatMemberHandler, TypeHandler

from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, backend_failure, build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

REACHABILITY = "/api/v1/bot/telegram-reachability"
# UpdateFactory dates every message 1_700_000_000; bot_membership() defaults to 100 s later.
MESSAGE_AT = "2023-11-14T22:13:20+00:00"
MEMBERSHIP_AT = "2023-11-14T22:15:00+00:00"
HELD_BLOCK = datetime(2023, 11, 14, 22, 0, tzinfo=UTC)


@pytest.fixture
async def bot(monkeypatch):
    return await build_bot_harness(monkeypatch)


@pytest.fixture
def user(bot):
    return bot.updates()


def reports(bot):
    return [(call.data, call.sign) for call in bot.backend.calls if call.endpoint == REACHABILITY]


def _report(status, at, source):
    return {"telegram_id": str(DEFAULT_USER_ID), "status": status, "at": at, "source": source}


async def test_blocking_the_bot_reports_blocked_signed_with_the_update_date(bot, user):
    await bot.send(user.bot_membership("kicked"))

    assert reports(bot) == [(_report("blocked", MEMBERSHIP_AT, "my_chat_member"), True)]
    assert bot.telegram.shown == [], "a blocked chat takes no messages"


async def test_coming_back_to_the_bot_reports_reachable(bot, user):
    await bot.send(user.bot_membership("member", old_status="kicked"))

    assert reports(bot) == [(_report("reachable", MEMBERSHIP_AT, "my_chat_member"), True)]


async def test_a_group_membership_change_is_not_a_customer_block(bot, user):
    await bot.send(user.bot_membership("kicked", chat_type="group"))

    assert reports(bot) == []


async def test_a_message_from_a_customer_held_as_blocked_reports_them_reachable(bot, user):
    """§7.2 b: a restart dropped the `member` update; the next message is the evidence."""
    bot.database.user["telegram_blocked_at"] = HELD_BLOCK

    await bot.send(user.command("menu"))

    assert reports(bot) == [(_report("reachable", MESSAGE_AT, "recovery"), True)]
    assert bot.telegram.shown, "the update still reaches its own handler"


async def test_a_customer_not_held_as_blocked_costs_no_report(bot, user):
    await bot.send(user.command("menu"))

    assert reports(bot) == []


async def test_the_recovery_middleware_leaves_membership_updates_to_their_handler(bot, user):
    """Exactly ONE report for a `member` update, even while a block is held: from the handler."""
    bot.database.user["telegram_blocked_at"] = HELD_BLOCK

    await bot.send(user.bot_membership("member", old_status="kicked"))

    assert reports(bot) == [(_report("reachable", MEMBERSHIP_AT, "my_chat_member"), True)]


async def test_a_refused_report_never_breaks_the_customers_update(bot, user):
    bot.database.user["telegram_blocked_at"] = HELD_BLOCK
    bot.backend.route("POST", REACHABILITY, lambda _call: backend_failure("Invalid signature", 401))

    await bot.send(user.command("menu"))

    assert len(reports(bot)) == 1
    assert bot.telegram.shown, "the menu still answers"


async def test_the_membership_update_is_claimed_by_the_chat_member_handler_alone(bot, user):
    claimed = bot.handlers_matching(user.bot_membership("kicked"))

    assert [(group, type(handler)) for group, handler in claimed] == [(1, ChatMemberHandler)]
    assert claimed[0][1].chat_member_types == ChatMemberHandler.MY_CHAT_MEMBER


async def test_the_recovery_middleware_sits_right_after_the_debug_logger(bot):
    recovery = [
        group
        for group, handlers in bot.application.handlers.items()
        for handler in handlers
        if isinstance(handler, TypeHandler) and handler.callback.__name__ == "report_recovery_middleware"
    ]

    assert recovery == [-9]
