"""After-commit pushes to drivers about bottle transfers and join requests, and the admins' dispute and reject alerts.

Call these ONLY after the write they announce has committed. Best-effort: the
work is already saved, so a broker that refuses the publish is logged, never
raised over it ("📥 Incoming transfers" and the session menu still show it).
"""

import logging
import os
from datetime import datetime
from typing import Any, Callable, Dict, List
from zoneinfo import ZoneInfo

from flask import current_app

from business_app.serializers.bottle_serializers import transfer_lines
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
    BOTTLE_EVENT_TRANSFER_REJECTED,
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

    `declared_quantity` is the full bottles, `declared_empty_quantity` the empties, and `lines` every line of the
    transfer (`transfer_lines`: full, empty, then each one-way unit), as the inbox publishes them. A bot that reads
    only the first two still renders the push.
    """
    _push(
        transfer.receiver_driver,
        BOTTLE_EVENT_TRANSFER_RECEIVED,
        {
            "id": transfer.id,
            "declared_quantity": transfer.declared_quantity,
            "declared_empty_quantity": transfer.declared_empty_quantity,
            "sender_name": _display_name(transfer.sender_driver),
            "lines": transfer_lines(transfer),
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
        {"id", "receiver_name", "confirmed_full", "confirmed_empty", "auto_closed", "lines"}
        `auto_closed` is True only when the sender's trip really closed after this count (T4).
    DISPUTED → `transfer_disputed`:
        {"id", "receiver_name", "declared_full", "declared_empty", "confirmed_full", "confirmed_empty",
         "admins_alerted", "lines"}
        `admins_alerted` is True only when the admins' Slack alerts channel took the dispute alert
        (`notify_admins_transfer_disputed`), so the message never says an admin will check a dispute nobody heard of.
    Full bottles are `declared_quantity` / `confirmed_quantity`; empties are the `*_empty_quantity` columns. `lines` is
    every line of the transfer with what was declared and counted (`transfer_lines`: full, empty, then each one-way
    unit); a bot that reads only the 18.9 L keys still renders the push.
    """
    receiver_name = _display_name(transfer.receiver_driver)
    confirmed = {"confirmed_full": transfer.confirmed_quantity, "confirmed_empty": transfer.confirmed_empty_quantity}
    if transfer.status == DriverBottleTransferStatus.CONFIRMED:
        payload = {
            "id": transfer.id,
            "receiver_name": receiver_name,
            **confirmed,
            "auto_closed": bool(auto_closed),
            "lines": transfer_lines(transfer),
        }
        _push(transfer.sender_driver, BOTTLE_EVENT_TRANSFER_CONFIRMED, payload)
        return
    payload = {
        "id": transfer.id,
        "receiver_name": receiver_name,
        "declared_full": transfer.declared_quantity,
        "declared_empty": transfer.declared_empty_quantity or 0,
        **confirmed,
        "admins_alerted": bool(admins_alerted),
        "lines": transfer_lines(transfer),
    }
    _push(transfer.sender_driver, BOTTLE_EVENT_TRANSFER_DISPUTED, payload)


def notify_transfer_rejected_to_sender(transfer, *, sender_trip_was_closed: bool) -> None:
    """The sender: the receiver rejected the transfer, so every line is back on the trip it left. Rendered by
    staff_bot/webhook_server.py::_render_bottle_event.

    `transfer_rejected`: {"id", "transfer_ref", "receiver_name", "lines", "sender_trip_was_closed"}
        `lines` is every line of the transfer as declared (`transfer_lines`: full, empty, then each one-way unit;
        nothing counted). `sender_trip_was_closed` is True when that trip had closed before the reject, so its
        discrepancies were recomputed.
    """
    payload = {
        "id": transfer.id,
        "transfer_ref": transfer.transfer_ref,
        "receiver_name": _display_name(transfer.receiver_driver),
        "lines": transfer_lines(transfer),
        "sender_trip_was_closed": bool(sender_trip_was_closed),
    }
    _push(transfer.sender_driver, BOTTLE_EVENT_TRANSFER_REJECTED, payload)


def _slack_text(text: str) -> str:
    """A driver's own text, safe in a Slack message: Slack reads `<...>` as a link or a mention (`<!channel>`
    notifies everyone) and `&` starts an entity, so these three are escaped the way Slack documents."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _signed(difference: int) -> str:
    """A count's difference as an admin reads it: -1, +3, and 0 for a match."""
    return f"{difference:+d}" if difference else "0"


def _slack_transfer_lines(
    lines: List[Dict[str, Any]], count: Callable[[Dict[str, Any]], int], joiner: str, *, signed: bool = False
) -> str:
    """THE Slack rendering of a transfer's lines: `lines` is `transfer_lines(transfer)` (full, empty, then each one-way
    unit, the order the bot renders them), every line printed and none skipped, joined by `joiner`. `count` picks the
    line's number (declared, counted, or a difference, which `signed` prints as `_signed` does). A line reads
    `{n} full`, `{n} empty` or `{n} × {product name}`, the name escaped for Slack (`_slack_text`)."""

    def rendered(line: Dict[str, Any]) -> str:
        number = _signed(count(line)) if signed else str(count(line))
        if line["kind"] == "product":
            return f"{number} × {_slack_text(line['product_name'])}"
        return f"{number} {line['kind']}"  # the kind is the word: "full" or "empty"

    return joiner.join(rendered(line) for line in lines)


def _slack_alert_footer(action: str) -> str:
    """The last two lines of a transfer alert: the time in the display timezone with the environment, then `action`
    on the admin site's Bottle Tracking page (config `ADMIN_UI_BASE_URL`), Bottle Transfers tab, with its link."""
    timestamp = datetime.now(ZoneInfo(DISPLAY_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %z")
    admin_site = (current_app.config.get("ADMIN_UI_BASE_URL") or "").rstrip("/")
    return (
        f"Time: {timestamp} | Env: {os.environ.get('FLASK_ENV', 'unknown')}\n"
        f"Action: {action} on the admin Bottle Tracking page, Bottle Transfers tab: {admin_site}{BOTTLE_TRACKING_PATH}"
    )


def _slack_trip_discrepancies(session) -> str:
    """A closed trip's two discrepancies as an admin reads them, each signed like a difference (`_signed`): the
    bottles' one (full and empty), then each line's that has its return written, by product name."""
    parts = [f"bottles {_signed(session.discrepancy or 0)}"]
    parts.extend(
        f"{_slack_text(line.product.name)} {_signed(line.discrepancy)}"
        for line in session.unit_lines
        if line.discrepancy is not None
    )
    return ", ".join(parts)


def notify_admins_transfer_disputed(transfer) -> bool:
    """Post the dispute to the admins' Slack alerts channel (`slack_alerts.send_slack_alert`, the #alerts-prod webhook
    every other application alert uses; owner decision 2026-11-10). Returns True only when Slack accepted the post: the
    sender's message and the receiver's answer say an admin will check only then.

    One message, in English, in the house style of `FiscalizationAlertService._send_slack`: the title with the
    transfer's ref, one fact per line (the sender and what he declared, the receiver and what he counted, the
    difference per line, counted minus declared; every line of the transfer through `_slack_transfer_lines`, so a
    dispute on a one-way unit line alone names that line), the time in the display timezone with the environment, and
    where to resolve it: the admin site's Bottle Tracking page (config `ADMIN_UI_BASE_URL`), Bottle Transfers tab.
    The helper never raises and never logs the webhook; the caller still runs this as a best-effort step after the
    count's commit (`BottleTrackingService._after_transfer_counted`).
    """
    lines = transfer_lines(transfer)
    declared = _slack_transfer_lines(lines, lambda line: line["declared"], " + ")
    counted = _slack_transfer_lines(lines, lambda line: line["confirmed"], " + ")
    difference = _slack_transfer_lines(lines, lambda line: line["confirmed"] - line["declared"], ", ", signed=True)
    text = (
        f"⚠️ *Bottle transfer disputed* — {transfer.transfer_ref}\n"
        f"Sender: {_slack_text(_display_name(transfer.sender_driver))} declared {declared}\n"
        f"Receiver: {_slack_text(_display_name(transfer.receiver_driver))} counted {counted}\n"
        f"Difference (counted − declared): {difference}\n"
        f"{_slack_alert_footer('resolve it')}"
    )
    return slack_alerts.send_slack_alert(text)


def notify_admins_transfer_rejected(transfer, *, sender_trip_was_closed: bool) -> bool:
    """Post the reject to the admins' Slack alerts channel, every time (spec 2026-11-18 §6.7: a reject puts the units
    back on one driver's word, so the office hears of each one). Returns True only when Slack accepted the post.

    The dispute's house style (`notify_admins_transfer_disputed`), in English: the title with the transfer's ref, the
    sender and every line he declared (`_slack_transfer_lines`), the receiver who rejected it, the sender's trip (still
    open, or closed before the reject with both its discrepancies as this reject recomputed them), the time with the
    environment, and the link to the Bottle Transfers tab. Run as a best-effort step after the reject's commit
    (`BottleTrackingService._after_transfer_rejected`).
    """
    declared = _slack_transfer_lines(transfer_lines(transfer), lambda line: line["declared"], " + ")
    trip = (
        f"closed before the reject; discrepancies recomputed: {_slack_trip_discrepancies(transfer.sender_session)}"
        if sender_trip_was_closed
        else "still open"
    )
    text = (
        f"❌ *Bottle transfer rejected* — {transfer.transfer_ref}\n"
        f"Sender: {_slack_text(_display_name(transfer.sender_driver))} declared {declared}\n"
        f"Receiver: {_slack_text(_display_name(transfer.receiver_driver))} rejected it, so every line is back on the "
        "sender's trip\n"
        f"Sender's trip: {trip}\n"
        f"{_slack_alert_footer('review it')}"
    )
    return slack_alerts.send_slack_alert(text)
