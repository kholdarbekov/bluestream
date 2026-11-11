"""After-commit pushes to drivers about bottle transfers and join requests, and the admins' dispute alert.

Call these ONLY after the write they announce has committed. Best-effort: the
work is already saved, so a broker that refuses the publish is logged, never
raised over it ("📥 Incoming transfers" and the session menu still show it).
"""

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import current_app

from business_app.tasks.staff_tasks import push_bottle_event
from business_app.utils import slack_alerts
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import DriverBottleTransferStatus
from shared.staff_constants import (
    BOTTLE_EVENT_JOIN_APPROVED,
    BOTTLE_EVENT_JOIN_DECLINED,
    BOTTLE_EVENT_JOIN_REQUESTED,
    BOTTLE_EVENT_TRANSFER_CONFIRMED,
    BOTTLE_EVENT_TRANSFER_DISPUTED,
    BOTTLE_EVENT_TRANSFER_RECEIVED,
)

logger = logging.getLogger(__name__)

# The admin page where a disputed transfer is resolved (admin_ui: Bottle Tracking, its Bottle Transfers tab).
BOTTLE_TRACKING_PATH = "/delivery/bottles"


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
    """The receiver: bottles are waiting for their count. Keys match the inbox rows.

    `declared_quantity` is the full bottles, `declared_empty_quantity` the empties. A bot that reads only the first
    still renders the push.
    """
    _push(
        transfer.receiver_driver,
        BOTTLE_EVENT_TRANSFER_RECEIVED,
        {
            "id": transfer.id,
            "declared_quantity": transfer.declared_quantity,
            "declared_empty_quantity": transfer.declared_empty_quantity,
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


def notify_transfer_outcome_to_sender(transfer, *, auto_closed: bool, admins_alerted: bool = False) -> None:
    """The sender: what the receiver counted. Rendered by staff_bot/webhook_server.py::_render_bottle_event.

    CONFIRMED → `transfer_confirmed`:
        {"id", "receiver_name", "confirmed_full", "confirmed_empty", "auto_closed"}
        `auto_closed` is True only when the sender's trip really closed after this count (T4).
    DISPUTED → `transfer_disputed`:
        {"id", "receiver_name", "declared_full", "declared_empty", "confirmed_full", "confirmed_empty",
         "admins_alerted"}
        `admins_alerted` is True only when the admins' Slack alerts channel took the dispute alert
        (`notify_admins_transfer_disputed`), so the message never says an admin will check a dispute nobody heard of.
    Full bottles are `declared_quantity` / `confirmed_quantity`; empties are the `*_empty_quantity` columns.
    """
    receiver_name = _display_name(transfer.receiver_driver)
    confirmed = {"confirmed_full": transfer.confirmed_quantity, "confirmed_empty": transfer.confirmed_empty_quantity}
    if transfer.status == DriverBottleTransferStatus.CONFIRMED:
        payload = {"id": transfer.id, "receiver_name": receiver_name, **confirmed, "auto_closed": bool(auto_closed)}
        _push(transfer.sender_driver, BOTTLE_EVENT_TRANSFER_CONFIRMED, payload)
        return
    payload = {
        "id": transfer.id,
        "receiver_name": receiver_name,
        "declared_full": transfer.declared_quantity,
        "declared_empty": transfer.declared_empty_quantity or 0,
        **confirmed,
        "admins_alerted": bool(admins_alerted),
    }
    _push(transfer.sender_driver, BOTTLE_EVENT_TRANSFER_DISPUTED, payload)


def _slack_text(text: str) -> str:
    """A driver's own text, safe in a Slack message: Slack reads `<...>` as a link or a mention (`<!channel>`
    notifies everyone) and `&` starts an entity, so these three are escaped the way Slack documents."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _signed(difference: int) -> str:
    """A count's difference as an admin reads it: -1, +3, and 0 for a match."""
    return f"{difference:+d}" if difference else "0"


def notify_admins_transfer_disputed(transfer) -> bool:
    """Post the dispute to the admins' Slack alerts channel (`slack_alerts.send_slack_alert`, the #alerts-prod webhook
    every other application alert uses; owner decision 2026-11-10). Returns True only when Slack accepted the post: the
    sender's message and the receiver's answer say an admin will check only then.

    One message, in English, in the house style of `FiscalizationAlertService._send_slack`: the title with the
    transfer's ref, one fact per line (the sender and what he declared, the receiver and what he counted, the
    difference per component, counted minus declared), the time in the display timezone with the environment, and
    where to resolve it: the admin site's Bottle Tracking page (config `ADMIN_UI_BASE_URL`), Bottle Transfers tab.
    The helper never raises and never logs the webhook; the caller still runs this as a best-effort step after the
    count's commit (`BottleTrackingService._after_transfer_counted`).
    """
    declared = (transfer.declared_quantity, transfer.declared_empty_quantity or 0)
    counted = (transfer.confirmed_quantity, transfer.confirmed_empty_quantity)
    full_difference, empty_difference = counted[0] - declared[0], counted[1] - declared[1]
    timestamp = datetime.now(ZoneInfo(DISPLAY_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %z")
    admin_site = (current_app.config.get("ADMIN_UI_BASE_URL") or "").rstrip("/")
    text = (
        f"⚠️ *Bottle transfer disputed* — {transfer.transfer_ref}\n"
        f"Sender: {_slack_text(_display_name(transfer.sender_driver))} declared "
        f"{declared[0]} full + {declared[1]} empty\n"
        f"Receiver: {_slack_text(_display_name(transfer.receiver_driver))} counted "
        f"{counted[0]} full + {counted[1]} empty\n"
        f"Difference (counted − declared): {_signed(full_difference)} full, {_signed(empty_difference)} empty\n"
        f"Time: {timestamp} | Env: {os.environ.get('FLASK_ENV', 'unknown')}\n"
        "Action: resolve it on the admin Bottle Tracking page, Bottle Transfers tab: "
        f"{admin_site}{BOTTLE_TRACKING_PATH}"
    )
    return slack_alerts.send_slack_alert(text)
