"""After-commit pushes to drivers about bottle transfers and join requests.

Call these ONLY after the write they announce has committed. Best-effort: the
work is already saved, so a broker that refuses the publish is logged, never
raised over it ("📥 Incoming transfers" and the session menu still show it).
"""

import logging

from business_app.tasks.staff_tasks import push_bottle_event
from shared.staff_constants import (
    BOTTLE_EVENT_JOIN_APPROVED,
    BOTTLE_EVENT_JOIN_DECLINED,
    BOTTLE_EVENT_JOIN_REQUESTED,
    BOTTLE_EVENT_TRANSFER_RECEIVED,
)

logger = logging.getLogger(__name__)


def _display_name(user) -> str:
    return user.full_name if user else ""


def _push(recipient, event: str, payload: dict) -> None:
    if recipient is None or not recipient.telegram_id:
        logger.info("[BOTTLE] %s push skipped: user %s has no Telegram chat", event, getattr(recipient, "id", None))
        return
    try:
        push_bottle_event.delay(int(recipient.telegram_id), event, payload)
    except Exception:  # noqa: BLE001 - after commit; the bot's own screens still show it
        logger.exception("[BOTTLE] %s push for user %s was not queued", event, recipient.id)


def notify_transfer_received(transfer) -> None:
    """The receiver: bottles are waiting for their count. Keys match the inbox rows."""
    _push(
        transfer.receiver_driver,
        BOTTLE_EVENT_TRANSFER_RECEIVED,
        {
            "id": transfer.id,
            "declared_quantity": transfer.declared_quantity,
            "sender_name": _display_name(transfer.sender_driver),
        },
    )


def notify_join_requested(session, requester) -> None:
    """The session owner: a colleague asks to join; the push carries Approve / Decline."""
    _push(
        session.driver,
        BOTTLE_EVENT_JOIN_REQUESTED,
        {
            "session_id": session.id,
            "requester_id": requester.id,
            "requester_name": _display_name(requester),
        },
    )


def notify_join_answered(session, requester, *, approved: bool) -> None:
    """The requester: the owner's answer."""
    event = BOTTLE_EVENT_JOIN_APPROVED if approved else BOTTLE_EVENT_JOIN_DECLINED
    _push(requester, event, {"session_id": session.id, "owner_name": _display_name(session.driver)})
