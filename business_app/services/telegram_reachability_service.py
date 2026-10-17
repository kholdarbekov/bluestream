"""Can this customer be reached on Telegram? The ONE writer of `users.is_bot_active` and
`users.telegram_blocked_at` (spec 2026-10-09 §7.2, D8, D19).

Both fields move together:
- blocked   -> is_bot_active False, telegram_blocked_at = when Telegram says it happened;
- reachable -> is_bot_active True,  telegram_blocked_at NULL.

Reports arrive late and out of order (a restart drops pending updates, the recovery middleware
reports on the customer's next message, a send that hit 403 reports when its task runs), so a
`reachable` report OLDER than the block the row holds is ignored: a late report can never undo a
newer block. A second block report keeps the newest block instant, which keeps that guard tight.

Each change of block state appends a `blocked` / `unblocked` event (§10) with the source as its
`detail`. Nothing here commits except `record_from_bot`, the HTTP entry point: every other caller
(login, registration, link, a send task) owns its transaction, so the flag and its event commit or
roll back with it. tests/unit/test_telegram_reachability_writers_guard.py pins that nothing else
writes either field.
"""

import logging
from datetime import datetime
from typing import Optional

from business_app import db
from business_app.models.user import User
from business_app.utils.exceptions import ValidationError
from business_app.utils.timezone_utils import ensure_utc
from business_app.utils.transactions import transactional

logger = logging.getLogger(__name__)

# Who saw the change; recorded as the event's `detail`.
REACHABILITY_SOURCES = ("my_chat_member", "recovery", "send_410", "telegram_login", "register", "link")
# What the customer bot may report, and from where (POST /api/v1/bot/telegram-reachability).
BOT_REPORT_STATUSES = ("blocked", "reachable")
BOT_REPORT_SOURCES = ("my_chat_member", "recovery")
REACHABILITY_REPORT_INVALID = "TELEGRAM_REACHABILITY_INVALID"


def _held_block(user: User) -> Optional[datetime]:
    return ensure_utc(user.telegram_blocked_at) if user.telegram_blocked_at is not None else None


def _checked_source(source: str) -> None:
    if source not in REACHABILITY_SOURCES:
        raise ValueError(f"Unknown Telegram reachability source: {source!r}")


def _record(user: User, kind: str, source: str, at: datetime) -> None:
    # Imported here, not at module level: auth_service imports this module on every Telegram login
    # path, and the reorder-reminders package must not load with it.
    from business_app.services.reorder_reminders.events import record_event

    if user.id is None:
        db.session.add(user)
        db.session.flush()
    record_event(user_id=user.id, kind=kind, detail=source, payload={"at": at.isoformat()})


class TelegramReachabilityService:
    """The only writer of a customer's Telegram reachability."""

    @staticmethod
    def mark_blocked(user: User, *, source: str, at: datetime) -> bool:
        """The customer blocked the bot (or deleted their account) at `at`. True when this is news."""
        _checked_source(source)
        at = ensure_utc(at)
        held = _held_block(user)
        user.is_bot_active = False
        if held is not None:
            user.telegram_blocked_at = max(held, at)
            return False
        user.telegram_blocked_at = at
        _record(user, "blocked", source, at)
        return True

    @staticmethod
    def mark_reachable(user: User, *, source: str, at: datetime) -> bool:
        """The customer reached the bot at `at`. A no-op when the block held is newer than `at`."""
        _checked_source(source)
        at = ensure_utc(at)
        held = _held_block(user)
        if held is not None and held > at:
            logger.info(
                "Stale Telegram reachable report ignored: user_id=%s source=%s at=%s blocked_at=%s",
                user.id,
                source,
                at.isoformat(),
                held.isoformat(),
            )
            return False
        changed = held is not None or not user.is_bot_active
        user.is_bot_active = True
        user.telegram_blocked_at = None
        if held is not None:
            _record(user, "unblocked", source, at)
        return changed

    @staticmethod
    def copy_state(target: User, origin: User) -> None:
        """A merge moves `origin`'s Telegram chat onto `target`: both fields travel together.

        No event: `origin`'s own blocked/unblocked events are re-pointed to the survivor by the
        merge (`ReorderReminderActions.on_users_merged`), so the log stays complete and one block
        is never counted twice.
        """
        target.is_bot_active = origin.is_bot_active
        target.telegram_blocked_at = origin.telegram_blocked_at

    @staticmethod
    @transactional
    def record_from_bot(telegram_id: str, status: str, at: datetime, *, source: str = "my_chat_member") -> bool:
        """The customer bot's report (§7.2 a, b). Commits. An unknown telegram_id answers False."""
        if status not in BOT_REPORT_STATUSES or source not in BOT_REPORT_SOURCES:
            raise ValidationError(
                "Unknown Telegram reachability report",
                details={"status": status, "source": source},
                error_code=REACHABILITY_REPORT_INVALID,
            )
        user = User.query.filter_by(telegram_id=str(telegram_id)).first()
        if user is None:
            logger.info("Telegram reachability report for unknown telegram_id=%s ignored", telegram_id)
            return False
        if status == "blocked":
            return TelegramReachabilityService.mark_blocked(user, source=source, at=at)
        return TelegramReachabilityService.mark_reachable(user, source=source, at=at)
