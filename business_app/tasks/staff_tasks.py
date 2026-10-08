"""
Staff notification Celery tasks.
Handles sending notifications to staff bot via internal webhooks.
"""

import logging
import requests
import os
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4
from celery import shared_task

logger = logging.getLogger(__name__)

STAFF_BOT_WEBHOOK_URL = os.environ.get("STAFF_BOT_WEBHOOK_URL", "http://staff_bot:8081")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


def _get_webhook_headers():
    """Build webhook headers with HMAC signature."""

    return {
        "Content-Type": "application/json",
    }


def _send_staff_webhook(endpoint: str, data: dict, timeout: int = 10) -> bool:
    """Send webhook to staff bot's internal server."""
    url = f"{STAFF_BOT_WEBHOOK_URL}{endpoint}"
    try:
        payload = dict(data or {})
        payload.setdefault("event_id", f"{endpoint.strip('/').replace('/', '_')}:{uuid4().hex}")
        payload.setdefault("event_timestamp", datetime.now(timezone.utc).isoformat())

        # Build HMAC signature
        import hmac
        import hashlib
        import json

        body = json.dumps(payload).encode("utf-8")
        # Dedicated staff_bot HMAC secret only. No JWT_SECRET_KEY fallback —
        # that's the auth-token trust boundary, not the webhook one.
        secret = WEBHOOK_SECRET
        if not secret:
            logger.error(
                "WEBHOOK_SECRET is not configured; refusing to send unsigned webhook for endpoint %s",
                endpoint,
            )
            return False

        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

        headers = {
            "Content-Type": "application/json",
            "X-Bot-Webhook-Signature": signature,
        }

        response = requests.post(url, json=payload, headers=headers, timeout=timeout)
        if response.status_code == 200:
            logger.info(f"Staff webhook sent successfully: {endpoint}")
            return True
        else:
            logger.warning(f"Staff webhook failed: {endpoint} -> {response.status_code}: {response.text}")
            return False
    except requests.exceptions.ConnectionError:
        logger.warning(f"Staff bot not reachable at {url} - notification skipped")
        return False
    except Exception as e:
        logger.error(f"Error sending staff webhook {endpoint}: {e}")
        return False


@shared_task(name="staff.notify_new_order", bind=True, max_retries=2, default_retry_delay=30)
def notify_staff_new_order(self, order_id: int, order_info: dict = None, exclude_driver_user_id: int = None):
    """
    Notify delivery persons about a new order available for pickup.

    Args:
        order_id: ID of the new order
        order_info: Pre-built order info dict (order_number, district, etc.)
        exclude_driver_user_id: driver who already received the targeted
            diversion offer for this order (route-UX plan 2026-08-11 §7,
            Task 13) — must not get a second Accept button.
    """
    try:
        # Celery's ContextTask already runs this task inside app.app_context()
        # (see tasks/celery_app.py); do NOT build a second app with create_app()
        # here — it re-ran env validation per order and was pure overhead.
        from business_app import db
        from business_app.models.delivery import Delivery, DeliveryPerson
        from business_app.models.user import User
        from business_app.services.staff_service import StaffService

        # Read the delivery when the broadcast SENDS, not when it was queued. It
        # waits behind the diversion evaluator (and create_delivery's fallback),
        # and in that gap the order can be claimed, cancelled or moved to a later
        # day -- a RESCHEDULED row is driverless but not claimable (R3). Every
        # Accept on such a card could only be refused. No row at all means the
        # order was never released to drivers.
        delivery = Delivery.query.filter_by(order_id=order_id).first()
        if delivery is None or not StaffService.is_delivery_claimable(delivery):
            logger.info(
                "new-order broadcast skipped for order %s: delivery %s is not claimable (status=%s)",
                order_id,
                getattr(delivery, "id", None),
                getattr(getattr(delivery, "status", None), "value", None),
            )
            return

        # Get all active delivery persons who haven't muted notifications
        query = (
            db.session.query(User.telegram_id)
            .join(DeliveryPerson, DeliveryPerson.user_id == User.id)
            .filter(DeliveryPerson.notifications_muted == False, User.telegram_id.isnot(None), User.status == "active")
        )
        if exclude_driver_user_id is not None:
            query = query.filter(User.id != exclude_driver_user_id)
        delivery_persons = query.all()

        telegram_ids = [dp.telegram_id for dp in delivery_persons if dp.telegram_id]

        if not telegram_ids:
            logger.info(f"No delivery persons to notify for order {order_id}")
            return

        # Build order info if not provided. We need delivery_id so the
        # bot can render Accept/Decline buttons that route through the
        # standard accept flow.
        if not order_info:
            from business_app.models.order import Order
            from business_app.utils.address_helpers import get_address_line
            from business_app.utils.delivery_window import format_delivery_window

            order = Order.query.get(order_id)
            if not order:
                logger.warning(f"Order {order_id} not found for notification")
                return

            addr = order.delivery_address

            customer_name = f"{order.user.first_name} {order.user.last_name or ''}".strip() if order.user else ""
            items = [
                {
                    "product_name": oi.product.name if oi.product else "",
                    "quantity": oi.quantity,
                }
                for oi in (order.order_items or [])
            ]

            order_info = {
                "order_id": order_id,
                "delivery_id": delivery.id,
                "order_number": order.order_number,
                "customer_name": customer_name,
                "total_amount": float(order.total_amount) if order.total_amount else 0,
                "payment_method": order.payment_method.value if order.payment_method else "cash",
                "item_count": len(order.order_items) if order.order_items else 0,
                "items": items,
                "district": addr.district if addr else "",
                "address": get_address_line(addr),
                # `delivery_window` is the structured {start,end,kind,label}
                # payload staff_bot renders locally (see
                # staff_bot/utils/formatters.py:format_delivery_window_line).
                # The legacy `time_slot` shim it replaced is gone — Task 13
                # converted the last staff_bot reader.
                "delivery_window": format_delivery_window(order.delivery_window_start, order.delivery_window_end),
            }

        data = {
            "event_id": f"new_order:{self.request.id}",
            "order_id": order_id,
            "delivery_person_telegram_ids": telegram_ids,
            "order_info": order_info,
        }

        _send_staff_webhook("/internal/new-order", data)

    except Exception as e:
        logger.error(f"Error in notify_staff_new_order: {e}")
        raise self.retry(exc=e)


@shared_task(name="staff.notify_order_assigned", bind=True)
def notify_staff_order_assigned(self, telegram_id: str, order_info: dict):
    """Notify a delivery person that an order was assigned to them by admin."""
    _send_staff_webhook(
        "/internal/order-assigned",
        {
            "event_id": f"order_assigned:{self.request.id}",
            "telegram_id": telegram_id,
            "order_info": order_info,
        },
    )


@shared_task(name="staff.push_bottle_event", bind=True, max_retries=3, default_retry_delay=60)
def push_bottle_event(self, telegram_id: int, event: str, payload: dict):
    """Push one bottle event (transfer waiting, join request, its answer) to one driver.

    Retried like `sales.push_sales_event`, and keyed the same way: Celery keeps
    `request.id` across `self.retry()`, so a retry collapses to one message in
    the staff bot's dedup. NOT a fixed per-request id: the bot holds an id for
    24h, which would silently swallow a genuine re-request after a decline.
    """
    data = {"telegram_id": telegram_id, "event": event, "payload": payload}
    if self.request.id:
        data["event_id"] = f"bottle-event:{self.request.id}"
    if not _send_staff_webhook("/internal/bottle-event", data):
        raise self.retry(exc=RuntimeError(f"bottle-event webhook failed for {event}"))
    return {"success": True, "event": event, "telegram_id": telegram_id}


@shared_task(name="staff.notify_order_reassigned", bind=True)
def notify_staff_order_reassigned(self, old_telegram_id: str, new_telegram_id: str, order_info: dict):
    """Notify both old and new delivery persons about a reassignment."""
    _send_staff_webhook(
        "/internal/order-reassigned",
        {
            "event_id": f"order_reassigned:{self.request.id}",
            "old_telegram_id": old_telegram_id,
            "new_telegram_id": new_telegram_id,
            "order_info": order_info,
        },
    )


@shared_task(name="staff.notify_order_cancelled", bind=True)
def notify_staff_order_cancelled(self, telegram_id: str, order_info: dict):
    """Notify delivery person that an assigned order was cancelled."""
    if not telegram_id:
        return
    _send_staff_webhook(
        "/internal/order-cancelled",
        {
            "event_id": f"order_cancelled:{self.request.id}",
            "telegram_id": telegram_id,
            "order_info": order_info,
        },
    )


@shared_task(name="staff.notify_order_unassigned", bind=True)
def notify_staff_order_unassigned(self, telegram_id: str, order_info: dict):
    """Tell a driver that dispatch took an order off their route.

    Distinct from `notify_staff_order_cancelled`: the order is NOT cancelled.
    Without `order_info["rescheduled_to"]` it went back to today's pool and
    someone else will take it. With it (an ISO date an admin reschedule sets)
    it moved to that day, and the bot says so instead of the pool copy.
    Reusing the cancellation copy here would tell the driver something false.
    """
    if not telegram_id:
        return
    _send_staff_webhook(
        "/internal/order-unassigned",
        {
            "event_id": f"order_unassigned:{self.request.id}",
            "telegram_id": telegram_id,
            "order_info": order_info,
        },
    )


def _delivery_awaiting_new_date(delivery_id: int):
    """The delivery, read now, while it still awaits a new date (F1); otherwise None.

    Both alert tasks ask this every time they run. A queued alert can wait behind a
    re-date or an order cancel, and a card asking for a date that was already given
    only sends an operator to a refusal.
    """
    from business_app import db
    from business_app.models.delivery import Delivery
    from business_app.services.order_schedule_service import OrderScheduleService

    delivery = db.session.get(Delivery, delivery_id)
    if delivery is None or delivery.order is None or not OrderScheduleService.awaiting_new_date(delivery.order):
        return None
    return delivery


def _alert_chat_id(user) -> Optional[int]:
    """The chat the staff bot addresses `user` at, or None for a Telegram id that holds no
    number. Logged and skipped, so one bad row costs only its own alert."""
    try:
        return int(user.telegram_id)
    except (TypeError, ValueError):
        logger.warning("delivery-failed alert: user %s has an unusable telegram_id %r", user.id, user.telegram_id)
        return None


@shared_task(name="staff.notify_delivery_failed")
def notify_delivery_failed(delivery_id: int, actor_user_id: Optional[int]):
    """Alert the staff who can give a failed delivery a new date (F7).

    Queued by `StaffService.update_delivery_status` once a failure commits, from the
    driver bot and from the admin Delivery page alike. The admin panel has no push
    channel, so the staff bot is the only one that reaches a person.

    Recipients: operators the staff bot would let in, and active admins and managers,
    each with a Telegram id. A person who holds both is one recipient and counts as an
    operator, so they get the date buttons. The person who marked the failure already
    knows and is left out. Each recipient gets one `push_delivery_failed`, in their own
    language. Each recipient is isolated: a Telegram id that holds no number, or a publish
    the broker drops, loses that person's alert and nobody else's.

    The card carries no date bounds. The bot fetches them for the one delivery when an
    operator taps.
    """
    from business_app.models.user import User
    from business_app.services.staff_service import StaffService
    from business_app.utils.exceptions import ForbiddenError
    from shared.enums import UserRole

    delivery = _delivery_awaiting_new_date(delivery_id)
    if delivery is None:
        logger.info("delivery-failed alert skipped for delivery %s: it no longer awaits a new date", delivery_id)
        return {"success": False, "reason": "not_awaiting_new_date", "delivery_id": delivery_id}

    # telegram_id -> (user, is_operator). Operators go first, so a manager who is also
    # an operator keeps the operator's card.
    recipients = {}
    operators = (
        User.query.filter(
            StaffService.staff_role_member_filter(UserRole.OPERATOR.value),
            User.status == "active",
            User.telegram_id.isnot(None),
        )
        .order_by(User.id)
        .all()
    )
    for operator in operators:
        try:
            # The staff bot's own door: an operator it refuses could not open the card.
            StaffService.assert_staff_active(operator)
        except ForbiddenError:
            continue
        chat_id = _alert_chat_id(operator)
        if chat_id is not None:
            recipients.setdefault(chat_id, (operator, True))
    supervisors = (
        User.query.filter(
            User.role.in_([UserRole.ADMIN, UserRole.MANAGER]),
            User.status == "active",
            User.telegram_id.isnot(None),
        )
        .order_by(User.id)
        .all()
    )
    for supervisor in supervisors:
        chat_id = _alert_chat_id(supervisor)
        if chat_id is not None:
            recipients.setdefault(chat_id, (supervisor, False))

    attempts = delivery.delivery_attempts or 0
    alert = {
        # Who and where, from the builder the operator's failed list uses too.
        **StaffService.failed_delivery_card(delivery),
        # A code: the staff bot words it in the reader's language.
        "reason": delivery.failed_delivery_reason,
        "attempts": attempts,
    }
    queued = 0
    for telegram_id, (recipient, is_operator) in recipients.items():
        if recipient.id == actor_user_id:
            continue
        try:
            push_delivery_failed.delay(
                delivery.id,
                attempts,
                telegram_id,
                {
                    **alert,
                    # One id per failure and person. A retry collapses in the staff bot's
                    # dedup, and a failure after a re-date alerts again.
                    "event_id": f"delivery-failed:{delivery.id}:{attempts}:{telegram_id}",
                    "telegram_id": telegram_id,
                    "is_operator": is_operator,
                    "language": recipient.preferred_language or "uz",
                },
            )
        except Exception as exc:  # noqa: BLE001 -- one recipient must not cost the rest their alert
            logger.warning(
                "delivery-failed alert for delivery %s not queued for user %s: %s", delivery.id, recipient.id, exc
            )
            continue
        queued += 1
    return {"success": True, "delivery_id": delivery_id, "recipients": queued}


@shared_task(name="staff.push_delivery_failed", bind=True, max_retries=3, default_retry_delay=60)
def push_delivery_failed(self, delivery_id: int, attempts: int, telegram_id: int, payload: dict):
    """Send one delivery-failed alert to one staff member's chat (F7).

    Modelled on `sales.push_sales_event`: a send the staff bot did not take is
    retried. Celery replays the same arguments, so the retry carries the same
    `event_id`, and the staff bot's dedup collapses it if the first send did land.

    Re-checked on every run, retries included. Once the delivery has been re-dated,
    its order closed, or it failed again, this alert is stale. A second failure queues
    its own alert, with the new attempt count. A stale run sends nothing and is not
    retried.
    """
    delivery = _delivery_awaiting_new_date(delivery_id)
    if delivery is None or (delivery.delivery_attempts or 0) != attempts:
        logger.info("delivery-failed alert for delivery %s, attempt %s, not sent: it is stale", delivery_id, attempts)
        return {"success": False, "reason": "stale", "delivery_id": delivery_id}
    if not _send_staff_webhook("/internal/delivery-failed", payload):
        raise self.retry(exc=RuntimeError(f"delivery-failed webhook failed for delivery {delivery_id}"))
    return {"success": True, "delivery_id": delivery_id}
