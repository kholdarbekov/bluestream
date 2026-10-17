"""Telegram tells the bot when a customer blocks or comes back; the backend records it.

Spec 2026-10-09 §7.2 (a, b), D8, D19. Two reporters, one call
(`api_client.report_telegram_reachability`, HMAC-signed):

* `bot_membership_changed` handles Telegram's own `my_chat_member` update for a private chat:
  `kicked` -> blocked, `member` -> reachable, dated by the update itself.
* `report_recovery_middleware` covers the `my_chat_member` updates a restart drops
  (`drop_pending_updates`) and the `/start` of a registered customer, which never reaches the
  backend: any other update from a customer the backend still holds as blocked reports them
  reachable. It never stops dispatch and never raises.

The backend owns the rule. It ignores a `reachable` report older than the block it holds, so
reporting late or twice is safe.
"""

import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import ContextTypes

from api_client import api_client
from database import BotUserRepository, db_manager

logger = logging.getLogger('handlers')

_user_repository = BotUserRepository(db_manager)

# What a private chat's new member status means. Every other status (administrator, restricted,
# left, ...) belongs to group chats, which are not customer chats.
_REACHABILITY_BY_STATUS = {
    ChatMemberStatus.BANNED: "blocked",  # Telegram's "kicked": the customer blocked the bot
    ChatMemberStatus.MEMBER: "reachable",  # the customer (re)started the bot
}


async def bot_membership_changed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report a block or an unblock Telegram announced (§7.2 a). Sends the customer nothing."""
    change = update.my_chat_member
    if change is None or change.chat.type != ChatType.PRIVATE:
        return
    status = _REACHABILITY_BY_STATUS.get(change.new_chat_member.status)
    if status is None:
        return
    try:
        async with api_client as client:
            await client.report_telegram_reachability(
                str(change.from_user.id), status, change.date, source="my_chat_member"
            )
    except Exception as exc:
        logger.warning("Could not report Telegram %s for %s: %s", status, change.from_user.id, exc)


def _received_at(update: Update) -> datetime:
    """The instant this update is evidence of: a new message's own date, else the moment it arrived.

    A tap carries no date of its own; its message's date is when the BOT spoke, possibly before the
    block, which would make a genuine return look stale.
    """
    message = update.message
    if message is not None and message.date is not None:
        return message.date
    return datetime.now(timezone.utc)


async def report_recovery_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any update from a customer the backend holds as blocked proves they came back (§7.2 b)."""
    if update.my_chat_member is not None:
        return
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None or chat.type != ChatType.PRIVATE:
        return
    try:
        blocked_at = await _user_repository.get_telegram_blocked_at(user.id)
        if not isinstance(blocked_at, datetime):
            return
        async with api_client as client:
            await client.report_telegram_reachability(
                str(user.id), "reachable", _received_at(update), source="recovery"
            )
    except Exception as exc:
        logger.warning("Could not report Telegram reachability for %s: %s", user.id, exc)
